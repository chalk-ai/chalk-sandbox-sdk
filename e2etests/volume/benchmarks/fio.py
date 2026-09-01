"""Run fio against a Volume mounted in one sandbox and emit pyperf JSON."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
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
from e2etests.volume.benchmarks.results import run_benchmark
from e2etests.volume.benchmarks.runtime import run_sandbox

MOUNT_PATH = "/volume"
BENCHMARK_DIR = f"{MOUNT_PATH}/fio-benchmark"
FIO_SCRIPT_DEST = "/opt/chalk/benchmarks/fio-benchmark.sh"
FIO_SCRIPT_SOURCE = Path(__file__).with_name("payloads") / "fio.sh"
COMMIT_SCRIPT_DEST = "/opt/chalk/benchmarks/commit-volume.sh"
COMMIT_SCRIPT_SOURCE = Path(__file__).with_name("payloads") / "commit.sh"
RESULTS_DIR = "fio-benchmark/results"
LOG_PATH = f"{RESULTS_DIR}/benchmark.log"
COMMIT_RESULT_PATH = f"{RESULTS_DIR}/volume-commit.json"
COMPLETION_MARKER = "Raw fio JSON results:"
ENTRYPOINT = """
mkdir -p "$2/results"
"$1" "$2" >"$2/results/benchmark.log" 2>&1
benchmark_status=$?
commit_started_ns=$(date +%s%N)
/bin/sh "$4" "$3" "$2/results/benchmark.log"
commit_status=$?
commit_finished_ns=$(date +%s%N)
commit_elapsed_ns=$((commit_finished_ns - commit_started_ns))
printf '{"elapsed_ns":%s}\n' "$commit_elapsed_ns" >"$2/results/volume-commit.json"
"$3/.chalk-volume/commit"
metrics_commit_status=$?
if [ "$benchmark_status" -ne 0 ]; then
  exit "$benchmark_status"
fi
if [ "$commit_status" -ne 0 ]; then
  exit "$commit_status"
fi
exit "$metrics_commit_status"
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


class _IncompleteResultsError(ValueError):
    pass


@dataclass(frozen=True)
class CommittedResults:
    version_id: int
    log: str
    results: dict[str, dict[str, Any]]
    commit_seconds: float
    ready_seconds: float
    read_attempts: int


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
    parser.add_argument("--memory", default="1Gi")
    parser.add_argument("--timeout-secs", type=positive_int, default=3600)
    return parser.parse_args()


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _read_committed_results(
    volume: Volume, timeout_secs: int = 120, poll_interval: float = 2
) -> CommittedResults:
    started_at = time.monotonic()
    deadline = started_at + timeout_secs
    attempts = 0
    version_id: int | None = None
    while True:
        attempts += 1
        try:
            version = volume.latest_version()
            version_id = version.id
            snapshot = volume.at_version(version_id)
            log = snapshot.read_file(LOG_PATH).decode(errors="replace")
            if COMPLETION_MARKER not in log:
                raise _IncompleteResultsError("benchmark log is incomplete")

            results: dict[str, dict[str, Any]] = {}
            for name in RESULT_NAMES:
                path = f"{RESULTS_DIR}/{name}.json"
                results[name] = json.loads(snapshot.read_file(path))
            commit_result = json.loads(snapshot.read_file(COMMIT_RESULT_PATH))
            commit_elapsed_ns = commit_result.get("elapsed_ns")
            if not isinstance(commit_elapsed_ns, int) or commit_elapsed_ns < 0:
                raise _IncompleteResultsError("volume commit timing is invalid")
            return CommittedResults(
                version_id=version_id,
                log=log,
                results=results,
                commit_seconds=commit_elapsed_ns / 1_000_000_000,
                ready_seconds=time.monotonic() - started_at,
                read_attempts=attempts,
            )
        except (
            VolumeFileNotFoundError,
            json.JSONDecodeError,
            _IncompleteResultsError,
        ) as error:
            if time.monotonic() >= deadline:
                version_detail = (
                    f"latest version {version_id}"
                    if version_id is not None
                    else "no version"
                )
                raise TimeoutError(
                    f"Volume results did not become coherent within {timeout_secs} "
                    f"seconds ({version_detail}): {error}"
                ) from error
            time.sleep(poll_interval)


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
    committed: CommittedResults, args: argparse.Namespace
) -> dict[str, Any]:
    benchmarks = [
        fio_result_to_benchmark(name, committed.results[name]) for name in RESULT_NAMES
    ]
    benchmarks.append(
        {
            "metadata": {
                "name": "volume-commit",
                "unit": "second",
                "volume_version_id": committed.version_id,
            },
            "runs": [{"values": [committed.commit_seconds]}],
        }
    )
    benchmarks.append(
        {
            "metadata": {
                "name": "volume-results-ready",
                "unit": "second",
                "volume_version_id": committed.version_id,
                "volume_read_attempts": committed.read_attempts,
            },
            "runs": [{"values": [committed.ready_seconds]}],
        }
    )
    return {
        "version": "1.0",
        "metadata": {
            "workload": "fio-microbench",
            "fio_size": args.size,
            "fio_jobs": args.jobs,
            "fio_small_files": args.small_files,
            "fio_small_file_size": args.small_file_size,
            "fio_direct": int(args.direct),
            "sandbox_cpu": args.cpu,
            "sandbox_memory": args.memory,
            "volume_version_id": committed.version_id,
            "volume_commit_seconds": committed.commit_seconds,
            "volume_results_ready_seconds": committed.ready_seconds,
            "volume_read_attempts": committed.read_attempts,
        },
        "benchmarks": benchmarks,
    }


def main() -> None:
    args = parse_args()
    output_path = Path(os.environ["CHALK_BENCHMARK_PYPERF_OUTPUT"])

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
            .add_local_file(
                str(COMMIT_SCRIPT_SOURCE),
                COMMIT_SCRIPT_DEST,
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
                MOUNT_PATH,
                COMMIT_SCRIPT_DEST,
            ],
        ).run(timeout=600)
        print(f"Running fio in sandbox {sandbox.id} on volume {volume_name}")
        run_sandbox(
            sandbox,
            args.timeout_secs,
            "fio",
            log_volume=volume,
            log_path=LOG_PATH,
        )

        committed = _read_committed_results(volume)
        print(
            f"Volume commit completed in {committed.commit_seconds:.3f}s; "
            f"version {committed.version_id} became readable in "
            f"{committed.ready_seconds:.3f}s after {committed.read_attempts} attempt(s)",
            flush=True,
        )
        print(
            committed.log,
            end="" if committed.log.endswith("\n") else "\n",
            flush=True,
        )
        output_path.write_text(
            json.dumps(build_pyperf_results(committed, args), sort_keys=True)
        )
    finally:
        if sandbox is not None:
            try:
                sandbox.terminate()
            except SandboxNotFoundError:
                pass
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
    run_benchmark("volume-fio", main)
