"""Run fio against a Volume mounted in one sandbox and emit pyperf JSON."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
import uuid

from chalkcompute import Image, Sandbox, Volume, VolumeError, VolumeFileNotFoundError

MOUNT_PATH = "/volume"
BENCHMARK_DIR = f"{MOUNT_PATH}/fio-benchmark"
FIO_SCRIPT_DEST = "/opt/chalk/benchmarks/fio-benchmark.sh"
FIO_SCRIPT_SOURCE = Path(__file__).with_name("fio-benchmark.sh")
RESULTS_DIR = "fio-benchmark/results"
LOG_PATH = f"{RESULTS_DIR}/benchmark.log"
ENTRYPOINT = """
mkdir -p "$2/results"
"$1" "$2" >"$2/results/benchmark.log" 2>&1
"""
RESULT_NAMES = (
    "sequential-write",
    "sequential-read",
    "random-read-4k",
    "random-write-4k",
    "small-file-write",
    "small-file-read",
)
WRITE_RESULTS = {"sequential-write", "random-write-4k", "small-file-write"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--volume")
    parser.add_argument("--keep-volume", action="store_true")
    parser.add_argument("--size", default="1G")
    parser.add_argument("--jobs", type=positive_int, default=1)
    parser.add_argument("--small-files", type=positive_int, default=1000)
    parser.add_argument("--small-file-size", default="4k")
    parser.add_argument("--direct", choices=("0", "1"), default="1")
    parser.add_argument("--cpu", default="2")
    parser.add_argument("--memory", default="4Gi")
    parser.add_argument("--timeout-secs", type=positive_int, default=3600)
    return parser.parse_args()


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _wait_for_completion(sandbox: Sandbox, timeout_secs: int) -> tuple[str, str | None]:
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        info = sandbox.refresh()
        print(f"Sandbox {sandbox.id} status: {info.status}", flush=True)
        if info.status not in ("pending", "running"):
            return info.status, info.status_message
        time.sleep(30)
    raise TimeoutError(
        f"Sandbox {sandbox.id} did not complete within {timeout_secs} seconds"
    )


def _read_volume_file(volume: Volume, path: str, timeout_secs: int = 60) -> bytes:
    deadline = time.monotonic() + timeout_secs
    while True:
        try:
            return volume.read_file(path)
        except VolumeFileNotFoundError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


def _read_results(volume: Volume) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for name in RESULT_NAMES:
        path = f"{RESULTS_DIR}/{name}.json"
        results[name] = json.loads(_read_volume_file(volume, path))
    return results


def _percentile(latency: dict[str, Any], percentile: str) -> int | float | None:
    percentiles = latency.get("percentile", {})
    return percentiles.get(percentile)


def fio_result_to_benchmark(name: str, result: dict[str, Any]) -> dict[str, Any]:
    jobs = result.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError(f"fio result {name!r} has no jobs")
    job = jobs[0]
    direction_name = "write" if name in WRITE_RESULTS else "read"
    direction = job[direction_name]
    runtime_ms = direction.get("runtime") or job.get("job_runtime")
    if not isinstance(runtime_ms, (int, float)) or runtime_ms <= 0:
        raise ValueError(f"fio result {name!r} has no positive runtime")

    latency = direction.get("clat_ns", {})
    metadata: dict[str, Any] = {
        "name": name,
        "unit": "second",
        "fio_version": result.get("fio version", "unknown"),
        "fio_direction": direction_name,
        "fio_runtime_ms": runtime_ms,
        "fio_io_bytes": direction.get("io_bytes", 0),
        "fio_bw_bytes_per_sec": direction.get("bw_bytes", 0),
        "fio_iops": direction.get("iops", 0),
        "fio_clat_mean_ns": latency.get("mean", 0),
    }
    for label, key in (
        ("p50", "50.000000"),
        ("p95", "95.000000"),
        ("p99", "99.000000"),
    ):
        value = _percentile(latency, key)
        if value is not None:
            metadata[f"fio_clat_{label}_ns"] = value

    return {
        "metadata": metadata,
        "runs": [{"values": [runtime_ms / 1000]}],
    }


def build_pyperf_results(
    results: dict[str, dict[str, Any]], args: argparse.Namespace
) -> dict[str, Any]:
    return {
        "version": "1.0",
        "metadata": {
            "fio_size": args.size,
            "fio_jobs": args.jobs,
            "fio_small_files": args.small_files,
            "fio_small_file_size": args.small_file_size,
            "fio_direct": int(args.direct),
            "sandbox_cpu": args.cpu,
            "sandbox_memory": args.memory,
        },
        "benchmarks": [
            fio_result_to_benchmark(name, results[name]) for name in RESULT_NAMES
        ],
    }


def main() -> None:
    args = parse_args()
    output_path = os.environ.get("CHALK_BENCHMARK_PYPERF_OUTPUT")
    if not output_path:
        raise RuntimeError("CHALK_BENCHMARK_PYPERF_OUTPUT must be set")

    generated_volume = args.volume is None
    volume_name = args.volume or f"sandbox-volume-fio-{uuid.uuid4().hex[:12]}"
    volume = Volume(volume_name)
    sandbox: Sandbox | None = None
    try:
        volume.reload()
        image = (
            Image.debian_slim(python_version="3.12")
            .run_commands(
                "apt-get update && apt-get install -y --no-install-recommends fio "
                "&& rm -rf /var/lib/apt/lists/*"
            )
            .add_local_file(
                str(FIO_SCRIPT_SOURCE),
                FIO_SCRIPT_DEST,
                mode=0o755,
                strategy="copy",
            )
        )
        sandbox = Sandbox(
            image=image,
            cpu=args.cpu,
            memory=args.memory,
            env={
                "FIO_SIZE": args.size,
                "FIO_JOBS": str(args.jobs),
                "FIO_SMALL_FILES": str(args.small_files),
                "FIO_SMALL_FILE_SIZE": args.small_file_size,
                "FIO_DIRECT": args.direct,
            },
            volumes=[volume.mount(MOUNT_PATH)],
            entrypoint=[
                "/bin/sh",
                "-c",
                ENTRYPOINT,
                "fio-benchmark",
                FIO_SCRIPT_DEST,
                BENCHMARK_DIR,
            ],
        ).run(timeout=600)
        print(f"Running fio in sandbox {sandbox.id} on volume {volume_name}")
        status, status_message = _wait_for_completion(sandbox, args.timeout_secs)
        try:
            log = _read_volume_file(volume, LOG_PATH).decode(errors="replace")
        except VolumeFileNotFoundError:
            if status == "succeeded":
                raise
            print(f"No benchmark log found at {LOG_PATH}", file=sys.stderr)
        else:
            print(log, end="" if log.endswith("\n") else "\n", flush=True)
        if status != "succeeded":
            detail = f": {status_message}" if status_message else ""
            raise RuntimeError(f"fio sandbox completed with status {status}{detail}")
        results = _read_results(volume)
        Path(output_path).write_text(
            json.dumps(build_pyperf_results(results, args), sort_keys=True)
        )
    finally:
        if sandbox is not None:
            sandbox.terminate()
        if generated_volume and not args.keep_volume:
            try:
                volume.delete()
            except VolumeError as error:
                print(
                    f"Unable to delete benchmark volume {volume_name}: {error}",
                    file=sys.stderr,
                )
        volume.close()


if __name__ == "__main__":
    main()
