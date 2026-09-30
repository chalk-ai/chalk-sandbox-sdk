"""Shared FIO workload configuration and result conversion."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable

MOUNT_PATH = "/volume"
BENCHMARK_DIR = f"{MOUNT_PATH}/fio-benchmark"
FIO_SCRIPT_DEST = "/opt/chalk/benchmarks/fio-benchmark.sh"
PAYLOADS = Path(__file__).parents[2] / "payloads"
FIO_SCRIPT_SOURCE = PAYLOADS / "fio" / "fio.sh"
COMMIT_SCRIPT_DEST = "/opt/chalk/benchmarks/commit-volume.sh"
RESULTS_DIR = "fio-benchmark/results"
LOG_PATH = f"{RESULTS_DIR}/benchmark.log"
COMMIT_RESULT_PATH = f"{RESULTS_DIR}/volume-commit.json"
COMPLETION_MARKER = "Raw fio JSON results:"
BASE_IMAGE = "python:3.12-slim-trixie"
INSTALL_FIO = (
    "apt-get update && apt-get install -y --no-install-recommends fio "
    "&& rm -rf /var/lib/apt/lists/*"
)
ENTRYPOINT = """
mkdir -p "$2/results"
/bin/sh "$1" "$2" >"$2/results/benchmark.log" 2>&1
benchmark_status=$?
cat "$2/results/benchmark.log"
commit_started_ns=$(date +%s%N)
/bin/sh "$4" "$3" "$2/results/benchmark.log"
commit_status=$?
commit_finished_ns=$(date +%s%N)
commit_elapsed_ns=$((commit_finished_ns - commit_started_ns))
printf '{"elapsed_ns":%s}\\n' "$commit_elapsed_ns" >"$2/results/volume-commit.json"
/bin/sh "$4" "$3" "$2/results/benchmark.log"
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


@dataclass(frozen=True)
class FioConfig:
    size: str = "1G"
    jobs: int = 1
    small_files: int = 1000
    small_file_size: str = "4k"
    direct: str = "1"
    cpu: str = "2"
    memory: str = "4Gi"
    timeout_secs: int = 3600

    def environment(self) -> dict[str, str]:
        return {
            "FIO_SIZE": self.size,
            "FIO_JOBS": str(self.jobs),
            "FIO_SMALL_FILES": str(self.small_files),
            "FIO_SMALL_FILE_SIZE": self.small_file_size,
            "FIO_DIRECT": self.direct,
        }

    def metadata(self) -> dict[str, Any]:
        return {
            "workload": "fio-microbench",
            "fio_size": self.size,
            "fio_jobs": self.jobs,
            "fio_small_files": self.small_files,
            "fio_small_file_size": self.small_file_size,
            "fio_direct": int(self.direct),
            "sandbox_cpu": self.cpu,
            "sandbox_memory": self.memory,
            "base_image": BASE_IMAGE,
            "fio_payload_sha256": hashlib.sha256(
                FIO_SCRIPT_SOURCE.read_bytes()
            ).hexdigest(),
            "cache_state": "unspecified",
        }


@dataclass(frozen=True)
class FioResult:
    results: dict[str, dict[str, Any]]
    log: str
    metadata: dict[str, Any]
    timings: dict[str, float] = field(default_factory=dict)


def entrypoint() -> list[str]:
    return [
        "/bin/sh",
        "-c",
        ENTRYPOINT,
        "fio-benchmark",
        FIO_SCRIPT_DEST,
        BENCHMARK_DIR,
        MOUNT_PATH,
        COMMIT_SCRIPT_DEST,
    ]


def read_results(
    read_file: Callable[[str], bytes],
) -> tuple[dict[str, dict[str, Any]], str, float]:
    log = read_file(LOG_PATH).decode(errors="replace")
    if COMPLETION_MARKER not in log:
        raise ValueError("benchmark log is incomplete")
    results = {
        name: json.loads(read_file(f"{RESULTS_DIR}/{name}.json"))
        for name in RESULT_NAMES
    }
    for name, result in results.items():
        fio_result_to_benchmark(name, result)
    commit_result = json.loads(read_file(COMMIT_RESULT_PATH))
    elapsed_ns = commit_result.get("elapsed_ns")
    if type(elapsed_ns) is not int or elapsed_ns < 0:
        raise ValueError("volume commit timing is invalid")
    return results, log, elapsed_ns / 1_000_000_000


def fio_result_to_benchmark(name: str, result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ValueError(f"fio result {name!r} is not an object")
    jobs = result.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError(f"fio result {name!r} has no jobs")
    if any(job.get("error", 0) != 0 for job in jobs):
        raise ValueError(f"fio result {name!r} contains an I/O error")
    if len(jobs) != 1:
        raise ValueError(f"fio result {name!r} must use group_reporting")
    job = jobs[0]
    direction_name = "write" if name in WRITE_RESULTS else "read"
    direction = job[direction_name]
    runtime_ms = direction.get("runtime") or job.get("job_runtime")
    if (
        not isinstance(runtime_ms, (int, float))
        or not math.isfinite(runtime_ms)
        or runtime_ms <= 0
    ):
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
        value = latency.get("percentile", {}).get(key)
        if value is not None:
            metadata[f"fio_clat_{label}_ns"] = value
    return {"metadata": metadata, "runs": [{"values": [runtime_ms / 1000]}]}


def build_pyperf_results(result: FioResult, config: FioConfig) -> dict[str, Any]:
    benchmarks = [
        fio_result_to_benchmark(name, result.results[name]) for name in RESULT_NAMES
    ]
    benchmarks.extend(
        {"metadata": {"name": name, "unit": "second"}, "runs": [{"values": [seconds]}]}
        for name, seconds in result.timings.items()
    )
    return {
        "version": "1.0",
        "metadata": {
            **config.metadata(),
            **result.metadata,
            "recorded_at": datetime.now(UTC).isoformat(),
        },
        "benchmarks": benchmarks,
    }
