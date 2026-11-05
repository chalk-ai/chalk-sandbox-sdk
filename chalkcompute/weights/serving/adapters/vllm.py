from __future__ import annotations

import sys
import json
import logging
import time
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout

from chalkcompute._weights_format import WeightManifest
from chalkcompute.weights.serving.profiling import validate_profiles

logger = logging.getLogger(__name__)


class VLLMAdapter:
    """vLLM 0.33 control endpoint; expose only the supervisor to callers."""

    def __init__(
        self,
        port: int = 8001,
        engine_args: list[str] | None = None,
        *,
        profile: bool = False,
    ):
        self.url = f"http://127.0.0.1:{port}"
        self.engine_args = engine_args or []
        self.profile = profile
        self.last_profile: dict | None = None
        self.expected_workers = self._parallel_size(
            {"--tensor-parallel-size", "-tp"}
        ) * self._parallel_size({"--pipeline-parallel-size", "-pp"})

    def _parallel_size(self, names: set[str]) -> int:
        size = 1
        for index, arg in enumerate(self.engine_args):
            option, _, inline = arg.partition("=")
            if option.replace("_", "-") in names:
                value = inline or self.engine_args[index + 1]
                size = int(value)
                if size < 1:
                    raise ValueError("Parallel sizes must be positive")
        return size

    def launch_cmd(self, model_dir: Path, port: int) -> list[str]:
        # Do not let passthrough arguments expose worker RPC or replace the model.
        protected = {
            "--host",
            "--port",
            "--model",
            "--worker-extension-cls",
            "--api-key",
            "--config",
        }
        if any(
            any(
                option.startswith(arg.split("=", 1)[0].replace("_", "-"))
                for option in protected
            )
            for arg in self.engine_args
            if arg.startswith("--")
        ):
            raise ValueError(
                "Engine args cannot override managed model, network, or extension options"
            )
        return [
            sys.executable,
            "-m",
            "vllm.entrypoints.openai.api_server",
            "--model",
            str(model_dir),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--worker-extension-cls",
            "chalkcompute.weights.serving.vllm_worker_ext.WeightSyncExtension",
            *self.engine_args,
        ]

    async def _post(self, path: str, body: dict) -> object:
        async with ClientSession(timeout=ClientTimeout(total=600)) as client:
            async with client.post(self.url + path, json=body) as response:
                response.raise_for_status()
                if response.content_type == "application/json":
                    result = await response.json()
                    if isinstance(result, dict) and (
                        result.get("error") or result.get("success") is False
                    ):
                        raise RuntimeError(f"vLLM control call failed: {result}")
                    return result
                return await response.text()

    async def apply(self, version_dir: Path, manifest: WeightManifest) -> None:
        self.last_profile = None
        started = time.perf_counter()
        result = await self._post(
            "/collective_rpc",
            {
                "method": "ws_apply_dir",
                "args": [
                    str(version_dir),
                    str(manifest.version),
                    *([True] if self.profile else []),
                ],
            },
        )
        # vLLM returns one JSON-compatible worker acknowledgement per rank.
        result = result.get("results") if isinstance(result, dict) else None
        if not isinstance(result, list) or len(result) != self.expected_workers:
            raise RuntimeError(f"Missing vLLM worker acknowledgements: {result!r}")
        for rank in result:
            if (
                not isinstance(rank, dict)
                or rank.get("version") != manifest.version
                or rank.get("ok") is not True
            ):
                raise RuntimeError(
                    f"Weight update was not acknowledged by a worker: {rank!r}"
                )
        if self.profile:
            workers = validate_profiles(result, manifest.version, self.expected_workers)
            self.last_profile = {
                "event": "chalk_weight_sync_profile",
                "phase": "apply",
                "version": manifest.version,
                "workers": workers,
                "collective_rpc_host_ms": (time.perf_counter() - started) * 1000,
            }
            logger.info("%s", json.dumps(self.last_profile, allow_nan=False))

    async def reset_cache(self) -> None:
        started = time.perf_counter()
        result = await self._post("/reset_prefix_cache", {})
        if not isinstance(result, dict) or result.get("success") is not True:
            raise RuntimeError(
                "vLLM did not acknowledge a successful prefix cache reset"
            )
        if self.profile:
            logger.info(
                "%s",
                json.dumps(
                    {
                        "event": "chalk_weight_sync_profile",
                        "phase": "cache_reset",
                        "version": self.last_profile["version"]
                        if self.last_profile
                        else None,
                        "host_ms": (time.perf_counter() - started) * 1000,
                    },
                    allow_nan=False,
                ),
            )

    async def ready(self) -> bool:
        try:
            async with ClientSession(timeout=ClientTimeout(total=5)) as client:
                async with client.get(self.url + "/health") as response:
                    return response.status == 200
        except (ClientError, OSError, TimeoutError):
            return False
