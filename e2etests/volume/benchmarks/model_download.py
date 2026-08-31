"""Benchmark scaled Hugging Face model reconstruction writes to a Volume."""

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

from chalkcompute import Image, Sandbox, SandboxNotFoundError, Volume, VolumeError

MOUNT_PATH = "/model-download"
WORKER_DEST = "/opt/chalk/benchmarks/model-download-worker.py"
WORKER_SOURCE = Path(__file__).with_name("payloads") / "model_download.py"
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
"$2/.chalk-volume/commit"
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


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


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


def _read_mode_result(
    volume: Volume, timeout_secs: int = 120, poll_interval: float = 2
) -> ModeResult:
    deadline = time.monotonic() + timeout_secs
    while True:
        try:
            version_id = volume.latest_version().id
            snapshot = volume.at_version(version_id)
            reconstruction = json.loads(snapshot.read_file(RESULT_PATH))
            commit = json.loads(snapshot.read_file(COMMIT_RESULT_PATH))
            elapsed_ns = commit["elapsed_ns"]
            if not isinstance(elapsed_ns, int) or elapsed_ns < 0:
                raise ValueError("commit timing is invalid")
            return ModeResult(
                version_id=version_id,
                reconstruction=reconstruction,
                commit_seconds=elapsed_ns / 1_000_000_000,
            )
        except (VolumeError, KeyError, ValueError, json.JSONDecodeError):
            if time.monotonic() >= deadline:
                raise TimeoutError("Model-download results did not become readable")
            time.sleep(poll_interval)


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
    image = Image("python:3.12-slim").add_local_file(
        str(WORKER_SOURCE), WORKER_DEST, mode=0o755, strategy="copy"
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
        ],
    ).run(timeout=600)
    try:
        status, status_message = _wait_for_completion(sandbox, args.timeout_secs)
        if status != "succeeded":
            detail = f": {status_message}" if status_message else ""
            raise RuntimeError(
                f"Model-download {mode} sandbox completed with status {status}{detail}"
            )
    finally:
        try:
            sandbox.terminate()
        except SandboxNotFoundError:
            pass
    return _read_mode_result(volume)


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
    volumes = {
        mode: Volume(f"sandbox-model-download-{mode}-{run_id}") for mode in MODES
    }
    try:
        for volume in volumes.values():
            volume.reload()
        results = {
            mode: _run_mode(mode, args, volumes[mode]) for mode in MODES
        }
        Path(output_path).write_text(
            json.dumps(build_pyperf_results(results, args), sort_keys=True)
        )
    finally:
        if not args.keep_volumes:
            for volume in volumes.values():
                _delete_volume(volume)
        for volume in volumes.values():
            volume.close()


if __name__ == "__main__":
    main()
