"""Benchmark buffered synthetic model-shard loads from a Volume."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from chalkcompute import (
    Image,
    Sandbox,
    Volume,
)
from e2etests.volume.benchmarks.harness import (
    positive_int,
    read_committed_json,
    read_committed_volume,
    run_benchmark,
    run_sandbox_workload,
    temporary_volume,
)

MODEL_MOUNT_PATH = "/model"
RESULTS_MOUNT_PATH = "/results"
SCRIPT_DEST = "/opt/chalk/benchmarks/model-load-benchmark.sh"
PAYLOADS = Path(__file__).parents[1] / "payloads"
SCRIPT_SOURCE = PAYLOADS / "model_load" / "model_load.sh"
COMMIT_SCRIPT_DEST = "/opt/chalk/benchmarks/commit-volume.sh"
COMMIT_SCRIPT_SOURCE = PAYLOADS / "commit.sh"
PREPARE_MARKER = "shard-count"

PREPARE_ENTRYPOINT = """
"$1" prepare "$2" >"$2/prepare.log" 2>&1
benchmark_status=$?
/bin/sh "$3" "$2" "$2/prepare.log"
commit_status=$?
"$2/.chalk-volume/commit"
log_commit_status=$?
if [ "$benchmark_status" -ne 0 ]; then
  exit "$benchmark_status"
fi
if [ "$commit_status" -ne 0 ]; then
  exit "$commit_status"
fi
exit "$log_commit_status"
"""

READ_ENTRYPOINT = """
mkdir -p "$3"
"$1" read "$2" "$3" "$4" >"$3/jobs-$4.log" 2>&1
benchmark_status=$?
/bin/sh "$6" "$5" "$3/jobs-$4.log"
commit_status=$?
"$5/.chalk-volume/commit"
log_commit_status=$?
if [ "$benchmark_status" -ne 0 ]; then
  exit "$benchmark_status"
fi
if [ "$commit_status" -ne 0 ]; then
  exit "$commit_status"
fi
exit "$log_commit_status"
"""


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
        .add_local_file(
            str(COMMIT_SCRIPT_SOURCE),
            COMMIT_SCRIPT_DEST,
            mode=0o755,
            strategy="copy",
        )
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
            COMMIT_SCRIPT_DEST,
        ],
    )
    run_sandbox_workload(
        sandbox,
        timeout_secs=args.timeout_secs,
        phase="model preparation",
        log=(volume, "prepare.log"),
    )

    committed = read_committed_volume(
        volume,
        lambda snapshot: snapshot.read_file(PREPARE_MARKER),
        description="Prepared model marker",
    )
    if committed.value.decode().strip() != str(args.shards):
        raise RuntimeError("Prepared model shard count is invalid")
    return committed.version_id


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
            COMMIT_SCRIPT_DEST,
        ],
    )
    run_sandbox_workload(
        sandbox,
        timeout_secs=args.timeout_secs,
        phase=f"model read jobs={jobs}",
        log=(results, f"jobs-{jobs}/jobs-{jobs}.log"),
    )

    _ = read_committed_volume(
        results,
        lambda snapshot: snapshot.read_file(f"jobs-{jobs}/jobs-{jobs}.complete"),
        description=f"Model-load jobs={jobs} completion marker",
    )


def _read_results(volume: Volume, jobs: tuple[int, ...]) -> dict[str, dict[str, Any]]:
    names = result_names(jobs)
    expected_paths = {
        name: f"jobs-{name.rsplit('-', 1)[-1]}/{name}.json" for name in names
    }
    return read_committed_json(
        volume,
        expected_paths,
        description="Model-load results",
    ).value


def main() -> dict[str, Any]:
    args = parse_args()

    with ExitStack() as stack:
        model_volume = stack.enter_context(
            temporary_volume("sandbox-model-load", keep=args.keep_volumes)
        )
        results_volume = stack.enter_context(
            temporary_volume("sandbox-model-load-results", keep=args.keep_volumes)
        )
        image = _benchmark_image()
        model_version_id = _prepare_model_volume(model_volume, image, args)
        model_snapshot = model_volume.at_version(model_version_id)
        print(
            f"Prepared {args.shards} model shards at volume version {model_version_id}",
            flush=True,
        )

        for jobs in args.jobs:
            print(f"Running fresh and warm reads with {jobs} job(s)", flush=True)
            _run_read_passes(model_snapshot, results_volume, image, args, jobs)

        results = _read_results(results_volume, args.jobs)
        return build_pyperf_results(results, args, model_version_id)


if __name__ == "__main__":
    run_benchmark("volume-model-load", main)
