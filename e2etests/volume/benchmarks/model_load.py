"""Benchmark buffered synthetic model-shard loads from a Volume."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
import uuid

from chalkcompute import (
    Image,
    Sandbox,
    SandboxNotFoundError,
    Volume,
    VolumeError,
    VolumeFileNotFoundError,
)

MODEL_MOUNT_PATH = "/model"
RESULTS_MOUNT_PATH = "/results"
SCRIPT_DEST = "/opt/chalk/benchmarks/model-load-benchmark.sh"
SCRIPT_SOURCE = Path(__file__).with_name("payloads") / "model_load.sh"
PREPARE_MARKER = "shard-count"

PREPARE_ENTRYPOINT = """
"$1" prepare "$2" >"$2/prepare.log" 2>&1
benchmark_status=$?
"$2/.chalk-volume/commit"
commit_status=$?
if [ "$benchmark_status" -ne 0 ]; then
  exit "$benchmark_status"
fi
exit "$commit_status"
"""

READ_ENTRYPOINT = """
mkdir -p "$3"
"$1" read "$2" "$3" "$4" >"$3/jobs-$4.log" 2>&1
benchmark_status=$?
"$5/.chalk-volume/commit"
commit_status=$?
if [ "$benchmark_status" -ne 0 ]; then
  exit "$benchmark_status"
fi
exit "$commit_status"
"""


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_jobs(value: str) -> tuple[int, ...]:
    try:
        jobs = tuple(int(item) for item in value.split(","))
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "must be positive comma-separated integers"
        ) from error
    if not jobs or any(job < 1 for job in jobs):
        raise argparse.ArgumentTypeError("must be positive comma-separated integers")
    if len(set(jobs)) != len(jobs):
        raise argparse.ArgumentTypeError("must not contain duplicates")
    return jobs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shards", type=positive_int, default=4)
    parser.add_argument("--shard-size", default="256M")
    parser.add_argument("--block-size", default="16M")
    parser.add_argument("--jobs", type=parse_jobs, default=(1, 4))
    parser.add_argument("--cpu", default="2")
    parser.add_argument("--memory", default="4Gi")
    parser.add_argument("--timeout-secs", type=positive_int, default=1800)
    parser.add_argument("--keep-volumes", action="store_true")
    args = parser.parse_args()
    if max(args.jobs) > args.shards:
        parser.error("--jobs values must not exceed --shards")
    return args


def result_names(jobs: tuple[int, ...]) -> tuple[str, ...]:
    return tuple(
        name
        for job_count in jobs
        for name in (
            f"fresh-read-jobs-{job_count}",
            f"warm-read-jobs-{job_count}",
        )
    )


def _wait_for_completion(
    sandbox: Sandbox, timeout_secs: int, poll_interval: float = 1
) -> tuple[str, str | None]:
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        info = sandbox.refresh()
        print(f"Sandbox {sandbox.id} status: {info.status}", flush=True)
        if info.status not in ("pending", "running"):
            return info.status, info.status_message
        time.sleep(poll_interval)
    raise TimeoutError(
        f"Sandbox {sandbox.id} did not complete within {timeout_secs} seconds"
    )


def _run_sandbox(sandbox: Sandbox, timeout_secs: int, phase: str) -> None:
    status, status_message = _wait_for_completion(sandbox, timeout_secs)
    if status == "succeeded":
        return
    detail = f": {status_message}" if status_message else ""
    raise RuntimeError(f"{phase} sandbox completed with status {status}{detail}")


def _wait_for_snapshot_file(
    volume: Volume,
    path: str,
    timeout_secs: int = 120,
    poll_interval: float = 2,
) -> tuple[int, bytes]:
    deadline = time.monotonic() + timeout_secs
    while True:
        try:
            version_id = volume.latest_version().id
            data = volume.at_version(version_id).read_file(path)
            return version_id, data
        except (VolumeError, VolumeFileNotFoundError):
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Volume file {path!r} did not become readable within "
                    f"{timeout_secs} seconds"
                )
            time.sleep(poll_interval)


def fio_result_to_benchmark(name: str, result: dict[str, Any]) -> dict[str, Any]:
    jobs = result.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError(f"fio result {name!r} has no jobs")
    read = jobs[0].get("read", {})
    runtime_ms = read.get("runtime") or jobs[0].get("job_runtime")
    if not isinstance(runtime_ms, (int, float)) or runtime_ms <= 0:
        raise ValueError(f"fio result {name!r} has no positive runtime")

    latency = read.get("clat_ns", {})
    job_count = int(name.rsplit("-", 1)[-1])
    metadata: dict[str, Any] = {
        "name": name,
        "unit": "second",
        "fio_version": result.get("fio version", "unknown"),
        "fio_direction": "read",
        "fio_jobs": job_count,
        "fio_runtime_ms": runtime_ms,
        "fio_io_bytes": read.get("io_bytes", 0),
        "fio_bw_bytes_per_sec": read.get("bw_bytes", 0),
        "fio_iops": read.get("iops", 0),
        "fio_clat_mean_ns": latency.get("mean", 0),
        "cache_state": "fresh-sandbox" if name.startswith("fresh-") else "warm",
    }
    percentiles = latency.get("percentile", {})
    for label, key in (
        ("p50", "50.000000"),
        ("p95", "95.000000"),
        ("p99", "99.000000"),
    ):
        if key in percentiles:
            metadata[f"fio_clat_{label}_ns"] = percentiles[key]

    return {
        "metadata": metadata,
        "runs": [{"values": [runtime_ms / 1000]}],
    }


def build_pyperf_results(
    results: dict[str, dict[str, Any]],
    args: argparse.Namespace,
    model_version_id: int,
) -> dict[str, Any]:
    names = result_names(args.jobs)
    return {
        "version": "1.0",
        "metadata": {
            "workload": "synthetic-model-load",
            "fio_shards": args.shards,
            "fio_shard_size": args.shard_size,
            "fio_block_size": args.block_size,
            "fio_jobs_list": ",".join(str(job) for job in args.jobs),
            "fio_direct": 0,
            "sandbox_cpu": args.cpu,
            "sandbox_memory": args.memory,
            "model_volume_version_id": model_version_id,
        },
        "benchmarks": [fio_result_to_benchmark(name, results[name]) for name in names],
    }


def _benchmark_image() -> Image:
    return (
        Image.debian_slim(python_version="3.12")
        .run_commands(
            "apt-get update && apt-get install -y --no-install-recommends fio "
            "&& rm -rf /var/lib/apt/lists/*"
        )
        .add_local_file(str(SCRIPT_SOURCE), SCRIPT_DEST, mode=0o755, strategy="copy")
    )


def _prepare_model_volume(
    volume: Volume, image: Image, args: argparse.Namespace
) -> int:
    sandbox = Sandbox(
        image=image,
        cpu=args.cpu,
        memory=args.memory,
        env={
            "FIO_SHARD_SIZE": args.shard_size,
            "FIO_SHARDS": str(args.shards),
            "FIO_BLOCK_SIZE": args.block_size,
        },
        volumes=[volume.mount(MODEL_MOUNT_PATH)],
        entrypoint=[
            "/bin/sh",
            "-c",
            PREPARE_ENTRYPOINT,
            "model-load-prepare",
            SCRIPT_DEST,
            MODEL_MOUNT_PATH,
        ],
    ).run(timeout=600)
    try:
        _run_sandbox(sandbox, args.timeout_secs, "model preparation")
    finally:
        try:
            sandbox.terminate()
        except SandboxNotFoundError:
            pass

    version_id, marker = _wait_for_snapshot_file(volume, PREPARE_MARKER)
    if marker.decode().strip() != str(args.shards):
        raise RuntimeError("Prepared model shard count is invalid")
    return version_id


def _run_read_passes(
    model: Volume,
    results: Volume,
    image: Image,
    args: argparse.Namespace,
    jobs: int,
) -> None:
    result_dir = f"{RESULTS_MOUNT_PATH}/jobs-{jobs}"
    sandbox = Sandbox(
        image=image,
        cpu=args.cpu,
        memory=args.memory,
        env={
            "FIO_SHARD_SIZE": args.shard_size,
            "FIO_SHARDS": str(args.shards),
            "FIO_BLOCK_SIZE": args.block_size,
        },
        volumes=[model.mount(MODEL_MOUNT_PATH), results.mount(RESULTS_MOUNT_PATH)],
        entrypoint=[
            "/bin/sh",
            "-c",
            READ_ENTRYPOINT,
            f"model-load-read-{jobs}",
            SCRIPT_DEST,
            MODEL_MOUNT_PATH,
            result_dir,
            str(jobs),
            RESULTS_MOUNT_PATH,
        ],
    ).run(timeout=600)
    try:
        _run_sandbox(sandbox, args.timeout_secs, f"model read jobs={jobs}")
    finally:
        try:
            sandbox.terminate()
        except SandboxNotFoundError:
            pass

    _wait_for_snapshot_file(results, f"jobs-{jobs}/jobs-{jobs}.complete")


def _read_results(
    volume: Volume, jobs: tuple[int, ...]
) -> dict[str, dict[str, Any]]:
    names = result_names(jobs)
    expected_paths = {
        name: f"jobs-{name.rsplit('-', 1)[-1]}/{name}.json" for name in names
    }
    deadline = time.monotonic() + 120
    while True:
        try:
            version_id = volume.latest_version().id
            snapshot = volume.at_version(version_id)
            return {
                name: json.loads(snapshot.read_file(path))
                for name, path in expected_paths.items()
            }
        except (VolumeError, VolumeFileNotFoundError, json.JSONDecodeError):
            if time.monotonic() >= deadline:
                raise TimeoutError("Model-load results did not become readable")
            time.sleep(2)


def _delete_volume(volume: Volume) -> None:
    try:
        volume.delete()
    except VolumeError as error:
        print(f"Unable to delete volume {volume.name}: {error}", file=sys.stderr)


def main() -> None:
    args = parse_args()
    output_path = os.environ.get("CHALK_BENCHMARK_PYPERF_OUTPUT")
    if not output_path:
        raise RuntimeError("CHALK_BENCHMARK_PYPERF_OUTPUT must be set")

    run_id = uuid.uuid4().hex[:12]
    model_volume = Volume(f"sandbox-model-load-{run_id}")
    results_volume = Volume(f"sandbox-model-load-results-{run_id}")
    try:
        model_volume.reload()
        results_volume.reload()
        image = _benchmark_image()
        model_version_id = _prepare_model_volume(model_volume, image, args)
        model_snapshot = model_volume.at_version(model_version_id)
        print(
            f"Prepared {args.shards} model shards at volume version "
            f"{model_version_id}",
            flush=True,
        )

        for jobs in args.jobs:
            print(f"Running fresh and warm reads with {jobs} job(s)", flush=True)
            _run_read_passes(model_snapshot, results_volume, image, args, jobs)

        results = _read_results(results_volume, args.jobs)
        Path(output_path).write_text(
            json.dumps(
                build_pyperf_results(results, args, model_version_id), sort_keys=True
            )
        )
    finally:
        if not args.keep_volumes:
            _delete_volume(results_volume)
            _delete_volume(model_volume)
        results_volume.close()
        model_volume.close()


if __name__ == "__main__":
    main()
