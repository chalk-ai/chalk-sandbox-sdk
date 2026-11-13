from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
from pathlib import Path

from aiohttp import web

from chalkcompute.weights.serving.adapters.sglang import SGLangAdapter
from chalkcompute.weights.serving.adapters.vllm import VLLMAdapter
from chalkcompute.weights.serving.full_checkpoint import FullCheckpointSyncer
from chalkcompute.weights.serving.managed_target import (
    ManagedTargetProvider,
    TargetProvider,
)
from chalkcompute.weights.serving.supervisor import AdmissionGate, Metrics, create_app
from chalkcompute.weights.serving.syncer import WeightSyncer

logger = logging.getLogger(__name__)


def _managed_target_provider(args: argparse.Namespace) -> TargetProvider | None:
    target_path = getattr(args, "managed_target_file", None) or os.environ.get(
        "CHALK_WEIGHT_TARGET_FILE"
    )
    source = getattr(args, "managed_target_source", None) or os.environ.get(
        "CHALK_WEIGHT_TARGET_SOURCE"
    )
    if source not in (None, "file", "model-deployment"):
        raise ValueError("Unsupported managed target source")
    if source == "model-deployment" and target_path:
        raise ValueError("Model deployment and local target file cannot be combined")
    if not target_path and source != "model-deployment":
        if source == "file":
            raise ValueError("File target source requires a managed target file")
        return None
    required = {
        "CHALK_MODEL_DEPLOYMENT_ID": "expected_deployment_id",
        "CHALK_MODEL_DEPLOYMENT_REVISION_ID": "expected_revision_id",
        "CHALK_WEIGHT_STREAM_ID": "expected_stream_id",
        "CHALK_WEIGHT_VOLUME_ID": "expected_volume_id",
    }
    if source == "model-deployment":
        required.update(
            {
                "CHALK_WEB_IDENTITY_TOKEN_FILE": "token_file",
                "CHALK_ENVIRONMENT_ID": "expected_environment_id",
            }
        )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ValueError(
            "Managed target source requires expected identity environment variables: "
            + ", ".join(missing)
        )
    if source == "model-deployment":
        from chalkcompute.weights.serving.model_target import (
            ModelDeploymentTargetProvider,
        )

        return ModelDeploymentTargetProvider(
            api_server=os.environ.get("CHALK_API_SERVER", "https://api.chalk.ai"),
            token_file=Path(os.environ["CHALK_WEB_IDENTITY_TOKEN_FILE"]),
            expected_environment_id=os.environ["CHALK_ENVIRONMENT_ID"],
            expected_deployment_id=os.environ["CHALK_MODEL_DEPLOYMENT_ID"],
            expected_revision_id=os.environ["CHALK_MODEL_DEPLOYMENT_REVISION_ID"],
            expected_stream_id=os.environ["CHALK_WEIGHT_STREAM_ID"],
            expected_volume_id=os.environ["CHALK_WEIGHT_VOLUME_ID"],
        )
    assert target_path is not None
    return ManagedTargetProvider(
        Path(target_path),
        expected_deployment_id=os.environ["CHALK_MODEL_DEPLOYMENT_ID"],
        expected_revision_id=os.environ["CHALK_MODEL_DEPLOYMENT_REVISION_ID"],
        expected_stream_id=os.environ["CHALK_WEIGHT_STREAM_ID"],
        expected_volume_id=os.environ["CHALK_WEIGHT_VOLUME_ID"],
    )


def _listen(value: str) -> tuple[str, int]:
    host, separator, port = value.rpartition(":")
    if not separator or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError("CHALK_LISTEN_ADDR must be HOST:PORT (for example :8000)")
    return host or "0.0.0.0", int(port)


async def serve(args: argparse.Namespace) -> None:
    engine = getattr(args, "engine", "vllm")
    if engine in ("vllm-sparse", "sglang-sparse") and (
        not getattr(args, "weight_layout", None)
        or not getattr(args, "weight_layout_sha256", None)
    ):
        raise ValueError("Sparse engines require an explicit layout file and SHA256")
    if engine == "sglang-sparse":
        from chalkcompute.weights.serving.adapters.sglang_resident import (
            SGLangResidentAdapter,
        )
        from chalkcompute.weights.serving.sparse_syncer import SparseWeightSyncer

        if getattr(args, "sglang_profile", None) is not None:
            raise ValueError("Sparse SGLang uses its fixed runtime profile")
        adapter = SGLangResidentAdapter(
            args.engine_port,
            args.engine_args,
            weight_layout=Path(args.weight_layout),
            weight_layout_sha256=args.weight_layout_sha256,
        )
        syncer_class = SparseWeightSyncer
    elif engine == "vllm-sparse":
        from chalkcompute.weights.serving.adapters.vllm_resident import (
            VLLMResidentAdapter,
        )
        from chalkcompute.weights.serving.sparse_syncer import SparseWeightSyncer

        if getattr(args, "sglang_profile", None) is not None:
            raise ValueError("SGLang profile requires --engine sglang-experimental")
        adapter = VLLMResidentAdapter(
            args.engine_port,
            args.engine_args,
            weight_layout=Path(args.weight_layout),
            weight_layout_sha256=args.weight_layout_sha256,
        )
        syncer_class = SparseWeightSyncer
    elif engine == "sglang-experimental":
        adapter = SGLangAdapter(
            args.engine_port,
            args.engine_args,
            diagnostic_profile=getattr(args, "sglang_profile", None)
            or "deterministic-triton",
        )
        syncer_class = FullCheckpointSyncer
    elif engine == "vllm":
        if getattr(args, "sglang_profile", None) is not None:
            raise ValueError("SGLang profile requires --engine sglang-experimental")
        adapter = VLLMAdapter(args.engine_port, args.engine_args)
        syncer_class = WeightSyncer
    else:
        raise ValueError(f"Unsupported serving engine: {engine}")
    gate = AdmissionGate()
    metrics = Metrics()
    managed_target = _managed_target_provider(args)
    if managed_target is not None and issubclass(syncer_class, FullCheckpointSyncer):
        raise ValueError(
            "Managed targets are not supported by the experimental full-checkpoint engine"
        )
    syncer = syncer_class(
        Path(args.weights),
        adapter,
        gate,
        metrics=metrics,
        poll_seconds=float(os.environ.get("CHALK_WEIGHT_SYNC_POLL_S", "2")),
        drain_timeout=float(os.environ.get("CHALK_WEIGHT_SYNC_DRAIN_TIMEOUT_S", "30")),
        keyframe_lag=int(os.environ.get("CHALK_WEIGHT_SYNC_KEYFRAME_LAG", "50")),
        managed_target=managed_target,
    )
    process: asyncio.subprocess.Process | None = None
    runner: web.AppRunner | None = None
    tasks: list[asyncio.Task] = []
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopped.set)

    async def launch(model_dir: Path) -> None:
        nonlocal process
        env = {**os.environ, "VLLM_SERVER_DEV_MODE": "1"}
        process = await asyncio.create_subprocess_exec(
            *adapter.launch_cmd(model_dir, args.engine_port),
            env=env,
            start_new_session=True,
        )
        deadline = loop.time() + args.startup_timeout
        while loop.time() < deadline:
            if process.returncode is not None:
                raise RuntimeError(
                    f"{engine} exited during startup ({process.returncode})"
                )
            if stopped.is_set():
                raise RuntimeError("Shutdown requested during startup")
            if await adapter.ready():
                return
            await asyncio.sleep(1)
        raise TimeoutError(f"{engine} startup deadline exceeded")

    async def stop_engine() -> None:
        if process is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 15)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()

    try:
        await syncer.bootstrap(launch)
        runner = web.AppRunner(
            create_app(gate, adapter.url, metrics), handler_cancellation=True
        )
        await runner.setup()
        host, port = _listen(os.environ.get("CHALK_LISTEN_ADDR", ":8000"))
        await web.TCPSite(runner, host, port).start()
        assert process is not None
        tasks = [
            asyncio.create_task(syncer.run(), name="weight-syncer"),
            asyncio.create_task(process.wait(), name="engine-exit"),
            asyncio.create_task(stopped.wait(), name="shutdown"),
            asyncio.create_task(gate.failed.wait(), name="unhealthy"),
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if not stopped.is_set():
            completed = ", ".join(sorted(task.get_name() for task in done))
            raise RuntimeError(
                "Live serving worker stopped or became unhealthy: "
                f"completed={completed}; reason={gate.failure_reason or 'unknown'}"
            )
    finally:
        await gate.fail()
        for task in tasks:
            task.cancel()
        if isinstance(syncer, FullCheckpointSyncer):
            # A cancelled preparation must join its writer before removing its
            # output. Stop GPU workers first even if a mounted read is stuck.
            await stop_engine()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Stop the entire process group, including engine/TP workers, before
        # waiting for HTTP cleanup: an abandoned stream may still be generating.
        if not isinstance(syncer, FullCheckpointSyncer):
            await stop_engine()
        if isinstance(syncer, FullCheckpointSyncer):
            syncer.close()
        if runner is not None:
            await runner.cleanup()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serve and synchronize a Chalk weight stream"
    )
    parser.add_argument(
        "--engine",
        choices=[
            "vllm",
            "vllm-sparse",
            "sglang-experimental",
            "sglang-sparse",
        ],
        default=os.environ.get("CHALK_ENGINE", "vllm"),
    )
    parser.add_argument(
        "--sglang-profile",
        choices=["deterministic-triton"],
        help="Pinned experimental SGLang profile (default: deterministic-triton)",
    )
    parser.add_argument("--weights", default=os.environ.get("CHALK_WEIGHT_STREAM_DIR"))
    parser.add_argument(
        "--weight-layout", help="Explicit serialized source-bound WeightLayout JSON"
    )
    parser.add_argument(
        "--weight-layout-sha256", help="SHA256 of the exact layout file bytes"
    )
    parser.add_argument("--engine-port", type=int, default=8001)
    parser.add_argument("--startup-timeout", type=float, default=1800)
    parser.add_argument(
        "--managed-target-source",
        choices=["file", "model-deployment"],
        default=os.environ.get("CHALK_WEIGHT_TARGET_SOURCE"),
        help="Use local test JSON or authenticated model-deployment polling",
    )
    parser.add_argument(
        "--managed-target-file",
        default=os.environ.get("CHALK_WEIGHT_TARGET_FILE"),
        help=(
            "Local/test target JSON file. Requires CHALK_MODEL_DEPLOYMENT_ID, "
            "CHALK_MODEL_DEPLOYMENT_REVISION_ID, CHALK_WEIGHT_STREAM_ID, and "
            "CHALK_WEIGHT_VOLUME_ID. No trusted controller delivery is included."
        ),
    )
    parser.add_argument("engine_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.engine in ("vllm-sparse", "sglang-sparse") and (
        not args.weight_layout or not args.weight_layout_sha256
    ):
        parser.error(
            "Sparse engines require --weight-layout and --weight-layout-sha256"
        )
    if not args.weights:
        parser.error("--weights or CHALK_WEIGHT_STREAM_DIR is required")
    if not 0 < args.engine_port < 65536 or args.startup_timeout <= 0:
        parser.error("Engine port and startup timeout must be positive")
    if args.engine_args[:1] == ["--"]:
        args.engine_args = args.engine_args[1:]
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve(args))


if __name__ == "__main__":
    main()
