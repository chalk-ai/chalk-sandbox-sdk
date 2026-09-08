"""Benchmark scaled Hugging Face model reconstruction writes to a Volume."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from chalkcompute import Image, Sandbox, Volume, VolumeError
from e2etests.volume.benchmarks.harness import (
    positive_int,
    read_committed_volume,
    run_benchmark,
    run_sandbox_workload,
    temporary_volume,
)

MOUNT_PATH = "/model-download"
WORKER_DEST = "/opt/chalk/benchmarks/model-download-worker.py"
PAYLOADS = Path(__file__).parents[1] / "payloads"
WORKER_SOURCE = PAYLOADS / "model_download" / "model_download.py"
COMMIT_SCRIPT_DEST = "/opt/chalk/benchmarks/commit-volume.sh"
COMMIT_SCRIPT_SOURCE = PAYLOADS / "commit.sh"
RESULT_PATH = "reconstruction-result.json"
COMMIT_RESULT_PATH = "commit-result.json"
MODES = ("direct", "sequential")

ENTRYPOINT = """
python3 "$1" \
  --mount-path "$2" \
  --mode "$3" \
  --large-shard-mib "$4" \
  --small-shard-mib "$5" \
  --extent-mib "$6" >"$2/reconstruction.log" 2>&1
benchmark_status=$?
commit_started_ns=$(date +%s%N)
/bin/sh "$7" "$2" "$2/reconstruction.log"
commit_status=$?
commit_finished_ns=$(date +%s%N)
commit_elapsed_ns=$((commit_finished_ns - commit_started_ns))
printf '{"elapsed_ns":%s}\n' "$commit_elapsed_ns" >"$2/commit-result.json"
"$2/.chalk-volume/commit"
metrics_commit_status=$?
if [ "$benchmark_status" -ne 0 ]; then
  exit "$benchmark_status"
fi
if [ "$commit_status" -ne 0 ]; then
  exit "$commit_status"
fi
exit "$metrics_commit_status"
"""


@dataclass(frozen=True)
class ModeResult:
    version_id: int
    reconstruction: dict[str, Any]
    commit_seconds: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--large-shard-mib", type=positive_int, default=1024)
    parser.add_argument("--small-shard-mib", type=positive_int, default=35)
    parser.add_argument("--extent-mib", type=positive_int, default=4)
    parser.add_argument("--cpu", default="2")
    parser.add_argument("--memory", default="2Gi")
    parser.add_argument("--timeout-secs", type=positive_int, default=1800)
    parser.add_argument("--keep-volumes", action="store_true")
    return parser.parse_args()


def _read_mode_result(
    volume: Volume, timeout_secs: int = 120, poll_interval: float = 2
) -> ModeResult:
    def read(snapshot: Volume) -> tuple[dict[str, Any], float]:
        reconstruction = json.loads(snapshot.read_file(RESULT_PATH))
        commit = json.loads(snapshot.read_file(COMMIT_RESULT_PATH))
        elapsed_ns = commit["elapsed_ns"]
        if not isinstance(reconstruction, dict):
            raise ValueError("reconstruction result is not an object")
        if not isinstance(elapsed_ns, int) or elapsed_ns < 0:
            raise ValueError("commit timing is invalid")
        return reconstruction, elapsed_ns / 1_000_000_000

    committed = read_committed_volume(
        volume,
        read,
        timeout_secs=timeout_secs,
        poll_interval=poll_interval,
        retry_errors=(VolumeError, KeyError, ValueError),
        description="Model-download results",
    )
    reconstruction, commit_seconds = committed.value
    return ModeResult(
        version_id=committed.version_id,
        reconstruction=reconstruction,
        commit_seconds=commit_seconds,
    )


def mode_benchmarks(mode: str, result: ModeResult) -> list[dict[str, Any]]:
    reconstruction = result.reconstruction
    elapsed_seconds = reconstruction["elapsed_seconds"]
    common = {
        "workload": "synthetic-model-download",
        "reconstruction_mode": mode,
        "io_direction": "write",
        "io_jobs": reconstruction["jobs"],
        "io_bytes": reconstruction["total_bytes"],
        "io_extent_bytes": reconstruction["extent_bytes"],
        "large_shard_bytes": reconstruction["large_shard_bytes"],
        "small_shard_bytes": reconstruction["small_shard_bytes"],
        "volume_version_id": result.version_id,
    }
    return [
        {
            "metadata": {
                "name": f"gemma-shaped-{mode}-write",
                "unit": "second",
                "io_bw_bytes_per_sec": reconstruction["bw_bytes_per_sec"],
                **common,
            },
            "runs": [{"values": [elapsed_seconds]}],
        },
        {
            "metadata": {
                "name": f"gemma-shaped-{mode}-commit",
                "unit": "second",
                **common,
            },
            "runs": [{"values": [result.commit_seconds]}],
        },
    ]


def build_pyperf_results(
    results: dict[str, ModeResult], args: argparse.Namespace
) -> dict[str, Any]:
    benchmarks = [
        benchmark
        for mode in MODES
        for benchmark in mode_benchmarks(mode, results[mode])
    ]
    return {
        "version": "1.0",
        "metadata": {
            "workload": "synthetic-model-download",
            "large_shard_mib": args.large_shard_mib,
            "small_shard_mib": args.small_shard_mib,
            "extent_mib": args.extent_mib,
            "sandbox_cpu": args.cpu,
            "sandbox_memory": args.memory,
        },
        "benchmarks": benchmarks,
    }


def _run_mode(mode: str, args: argparse.Namespace, volume: Volume) -> ModeResult:
    image = (
        Image("python:3.12-slim")
        .add_local_file(str(WORKER_SOURCE), WORKER_DEST, mode=0o755, strategy="copy")
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
        volumes=[volume.mount(MOUNT_PATH)],
        entrypoint=[
            "/bin/sh",
            "-c",
            ENTRYPOINT,
            f"model-download-{mode}",
            WORKER_DEST,
            MOUNT_PATH,
            mode,
            str(args.large_shard_mib),
            str(args.small_shard_mib),
            str(args.extent_mib),
            COMMIT_SCRIPT_DEST,
        ],
    )
    run_sandbox_workload(
        sandbox,
        timeout_secs=args.timeout_secs,
        phase=f"model-download {mode}",
        log=(volume, "reconstruction.log"),
    )
    return _read_mode_result(volume)


def main() -> dict[str, Any]:
    args = parse_args()

    with (
        temporary_volume(
            "sandbox-model-download-direct", keep=args.keep_volumes
        ) as direct_volume,
        temporary_volume(
            "sandbox-model-download-sequential", keep=args.keep_volumes
        ) as sequential_volume,
    ):
        volumes = {"direct": direct_volume, "sequential": sequential_volume}
        results = {mode: _run_mode(mode, args, volumes[mode]) for mode in MODES}
        return build_pyperf_results(results, args)


if __name__ == "__main__":
    run_benchmark("volume-model-download", main)
