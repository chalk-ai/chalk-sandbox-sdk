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
from chalkcompute.weights.serving.supervisor import AdmissionGate, Metrics, create_app
from chalkcompute.weights.serving.syncer import WeightSyncer

logger = logging.getLogger(__name__)


def _listen(value: str) -> tuple[str, int]:
    host, separator, port = value.rpartition(":")
    if not separator or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError("CHALK_LISTEN_ADDR must be HOST:PORT (for example :8000)")
    return host or "0.0.0.0", int(port)


async def serve(args: argparse.Namespace) -> None:
    engine = getattr(args, "engine", "vllm")
    if engine == "sglang-experimental":
        adapter = SGLangAdapter(args.engine_port, args.engine_args)
        syncer_class = FullCheckpointSyncer
    else:
        adapter = VLLMAdapter(args.engine_port, args.engine_args)
        syncer_class = WeightSyncer
    gate = AdmissionGate()
    metrics = Metrics()
    syncer = syncer_class(
        Path(args.weights),
        adapter,
        gate,
        metrics=metrics,
        poll_seconds=float(os.environ.get("CHALK_WEIGHT_SYNC_POLL_S", "2")),
        drain_timeout=float(os.environ.get("CHALK_WEIGHT_SYNC_DRAIN_TIMEOUT_S", "30")),
        keyframe_lag=int(os.environ.get("CHALK_WEIGHT_SYNC_KEYFRAME_LAG", "50")),
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
        await asyncio.gather(*tasks, return_exceptions=True)
        # Stop the entire process group, including engine/TP workers, before
        # waiting for HTTP cleanup: an abandoned stream may still be generating.
        if process is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 15)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
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
        choices=["vllm", "sglang-experimental"],
        default=os.environ.get("CHALK_ENGINE", "vllm"),
    )
    parser.add_argument("--weights", default=os.environ.get("CHALK_WEIGHT_STREAM_DIR"))
    parser.add_argument("--engine-port", type=int, default=8001)
    parser.add_argument("--startup-timeout", type=float, default=1800)
    parser.add_argument("engine_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
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
