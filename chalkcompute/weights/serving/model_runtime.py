"""Managed engine lifetime for the native remote-call server's explicit fence mode.

This module is loaded only by managed model replicas. It does not eagerly import Torch or
an inference engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chalkcompute.weights.serving.managed_target import (
    ModelTargetNotSelected,
    TargetProvider,
)
from chalkcompute.weights.serving.sparse_syncer import SparseWeightSyncer
from chalkcompute.weights.serving.supervisor import AdmissionGate, NativeServingHandle

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelServingConfig:
    engine: str
    engine_args: list[str]
    weights_dir: Path
    weight_layout: Path
    weight_layout_sha256: str
    engine_port: int
    startup_timeout_seconds: float
    initial_model_dir: Path | None = None

    @classmethod
    def from_json(cls, raw: str) -> ModelServingConfig:
        if len(raw.encode()) > 64 * 1024:
            raise ValueError("Managed serving config exceeds size bound")

        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate managed serving config field")
                result[key] = value
            return result

        value = json.loads(raw, object_pairs_hook=unique)
        required = {
            "engine",
            "engine_args",
            "weights_dir",
            "weight_layout",
            "weight_layout_sha256",
            "engine_port",
            "startup_timeout_seconds",
        }
        if (
            not isinstance(value, dict)
            or not required <= value.keys()
            or value.keys() - required - {"initial_model_dir"}
        ):
            raise ValueError("Exact managed serving config fields required")
        if value["engine"] not in ("SERVING_ENGINE_VLLM", "SERVING_ENGINE_SGLANG"):
            raise ValueError("Managed runtime requires a supported sparse engine")
        args = value["engine_args"]
        if not isinstance(args, list) or any(
            not isinstance(arg, str) or not arg or "\x00" in arg for arg in args
        ):
            raise ValueError("A list of engine arguments is required")
        for name in ("weights_dir", "weight_layout", "initial_model_dir"):
            if name in value and (
                not isinstance(value[name], str)
                or "\x00" in value[name]
                or not Path(value[name]).is_absolute()
                or value[name] == "/"
                or value[name].startswith("//")
                or os.path.normpath(value[name]) != value[name]
            ):
                raise ValueError(f"{name} must be an absolute path")
        digest = value["weight_layout_sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Exact weight layout SHA256 required")
        port = value["engine_port"]
        timeout = value["startup_timeout_seconds"]
        if type(port) is not int or not 1 <= port <= 65535 or port == 8080:
            raise ValueError("Engine port must differ from native server port 8080")
        if type(timeout) is not int or not 1 <= timeout <= 1800:
            raise ValueError("Startup timeout must be between 1 and 1800 seconds")
        return cls(
            "vllm-sparse"
            if value["engine"] == "SERVING_ENGINE_VLLM"
            else "sglang-sparse",
            list(args),
            Path(value["weights_dir"]),
            Path(value["weight_layout"]),
            digest,
            port,
            float(timeout),
            Path(value["initial_model_dir"]) if "initial_model_dir" in value else None,
        )


@dataclass(frozen=True)
class ModelServingBinding:
    environment_id: str
    model_scaling_group_id: str
    model_scaling_group_revision_id: str
    weight_stream_id: str
    volume_id: str

    @classmethod
    def from_dict(cls, value: object) -> ModelServingBinding:
        fields = set(cls.__dataclass_fields__)
        if (
            not isinstance(value, dict)
            or value.keys() != fields
            or any(
                not isinstance(item, str)
                or not item
                or item.strip() != item
                or "\x00" in item
                for item in value.values()
            )
        ):
            raise ValueError("Exact authoritative model serving binding required")
        return cls(**value)


@dataclass(frozen=True)
class ModelServingBootstrap:
    config: ModelServingConfig
    binding: ModelServingBinding

    @classmethod
    def from_json(cls, raw: str) -> ModelServingBootstrap:
        if len(raw.encode("utf-8")) > 64 * 1024:
            raise ValueError("Model serving bootstrap exceeds size bound")

        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate model serving bootstrap field")
                result[key] = value
            return result

        value = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(value, dict) or value.keys() != {
            "schema_version",
            "config",
            "binding",
        }:
            raise ValueError("Exact model serving bootstrap fields required")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("Unsupported model serving bootstrap schema")
        return cls(
            ModelServingConfig.from_json(json.dumps(value["config"])),
            ModelServingBinding.from_dict(value["binding"]),
        )


class ManagedModelRuntime:
    """Own one loopback engine and fence the native server before user imports."""

    def __init__(
        self,
        config: ModelServingConfig,
        serving_handle: NativeServingHandle,
        provider: TargetProvider,
    ) -> None:
        if not serving_handle.requires_target:
            raise ValueError("Managed runtime requires a strict native serving handle")
        self.config = config
        self.gate = AdmissionGate(serving_handle)
        self.gate.managed_target = True
        self.provider = provider
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self.failure: BaseException | None = None

    @classmethod
    def from_bootstrap(
        cls, raw: str, serving_handle: NativeServingHandle
    ) -> ManagedModelRuntime:
        bootstrap = ModelServingBootstrap.from_json(raw)
        from chalkcompute.weights.serving.model_target import (
            ModelDeploymentTargetProvider,
        )

        token_file = os.environ.get("CHALK_FUNCTION_QUEUE_TOKEN_FILE")
        if not token_file:
            raise ValueError("Managed runtime requires the platform FNQ token file")
        binding = bootstrap.binding
        provider = ModelDeploymentTargetProvider(
            api_server=os.environ.get("CHALK_API_SERVER", "https://api.chalk.ai"),
            token_file=Path(token_file),
            expected_environment_id=binding.environment_id,
            expected_deployment_id=binding.model_scaling_group_id,
            expected_revision_id=binding.model_scaling_group_revision_id,
            expected_stream_id=binding.weight_stream_id,
            expected_volume_id=binding.volume_id,
        )
        runtime = cls(bootstrap.config, serving_handle, provider)
        os.environ["CHALK_MODEL_ENGINE_URL"] = (
            f"http://127.0.0.1:{bootstrap.config.engine_port}"
        )
        return runtime

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Managed runtime already started")
        self._thread = threading.Thread(
            target=self._thread_main, name="chalk-model-runtime", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("Managed runtime loop did not initialize")
        if self.failure is not None:
            raise RuntimeError(
                "Managed runtime initialization failed"
            ) from self.failure

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except BaseException as error:
            self.failure = error
            self.gate._close_native_admission()
            logger.error(
                "Managed model runtime stopped; replica restart required (%s)",
                type(error).__name__,
            )
        finally:
            self._ready.set()

    async def _run(self) -> None:
        loop = self._loop = asyncio.get_running_loop()
        stop = self._stop = asyncio.Event()
        self._ready.set()
        config = self.config
        process: asyncio.subprocess.Process | None = None
        process_start: asyncio.Task[asyncio.subprocess.Process] | None = None
        tasks: list[asyncio.Task[Any]] = []

        async def launch(directory: Path) -> None:
            nonlocal process, process_start
            process_start = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *adapter.launch_cmd(directory, config.engine_port),
                    start_new_session=True,
                )
            )
            # Creation owns a process even when the bootstrap caller is cancelled.
            # Cleanup joins the shielded creator before inspecting its process group.
            process = await asyncio.shield(process_start)
            deadline = loop.time() + config.startup_timeout_seconds
            while loop.time() < deadline:
                if process.returncode is not None:
                    raise RuntimeError("Managed engine exited during startup")
                if stop.is_set():
                    raise RuntimeError("Managed engine startup interrupted")
                if await adapter.ready():
                    return
                await asyncio.sleep(0.25)
            raise TimeoutError("Managed engine startup deadline exceeded")

        try:
            # New revisions legitimately have no selected target. Poll without
            # loading a model or exposing readiness until the first authenticated lease.
            target_deadline = loop.time() + config.startup_timeout_seconds
            while not stop.is_set():
                try:
                    await asyncio.to_thread(self.provider.current)
                    break
                except ModelTargetNotSelected:
                    if loop.time() >= target_deadline:
                        raise TimeoutError(
                            "Initial managed target selection deadline exceeded"
                        )
                    try:
                        await asyncio.wait_for(stop.wait(), 2)
                    except TimeoutError:
                        pass
            if stop.is_set():
                return
            if config.engine == "vllm-sparse":
                from chalkcompute.weights.serving.adapters.vllm_resident import (
                    VLLMResidentAdapter,
                )

                adapter = VLLMResidentAdapter(
                    config.engine_port,
                    config.engine_args,
                    weight_layout=config.weight_layout,
                    weight_layout_sha256=config.weight_layout_sha256,
                )
            else:
                from chalkcompute.weights.serving.adapters.sglang_resident import (
                    SGLangResidentAdapter,
                )

                adapter = SGLangResidentAdapter(
                    config.engine_port,
                    config.engine_args,
                    weight_layout=config.weight_layout,
                    weight_layout_sha256=config.weight_layout_sha256,
                )
            syncer = SparseWeightSyncer(
                config.weights_dir, adapter, self.gate, managed_target=self.provider
            )
            bootstrap = asyncio.create_task(
                syncer.bootstrap(launch, initial_model_dir=config.initial_model_dir)
            )
            shutdown = asyncio.create_task(stop.wait())
            tasks = [bootstrap, shutdown]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if shutdown in done:
                return
            await bootstrap
            assert process is not None
            tasks.extend(
                [
                    asyncio.create_task(syncer.run()),
                    asyncio.create_task(process.wait()),
                    asyncio.create_task(self.gate.failed.wait()),
                ]
            )
            done, _ = await asyncio.wait(tasks[1:], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if not stop.is_set():
                raise RuntimeError(
                    "Managed engine or target synchronization became unhealthy"
                )
        finally:
            await self.gate.fail("Managed runtime stopped")
            for task in tasks:
                task.cancel()
            if process_start is not None:
                with contextlib.suppress(Exception):
                    process = await process_start
            if process is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
                # A launcher may exit before a worker which ignores SIGTERM.
                # Its isolated process group remains ours until all workers exit.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            await asyncio.gather(*tasks, return_exceptions=True)

    def close(self) -> None:
        self.gate._close_native_admission()
        if (
            self._loop is not None
            and self._stop is not None
            and self._loop.is_running()
        ):
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(20)
            if self._thread.is_alive():
                raise RuntimeError(
                    "Managed runtime did not stop within cleanup deadline"
                )
