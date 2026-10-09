"""Benchmark SQLite writes, reads, journals, and renames on a Volume."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from chalkcompute import Image, Sandbox, Volume
from e2etests.volume.benchmarks.harness import (
    BenchmarkFailure,
    positive_int,
    read_committed_json,
    run_benchmark,
    run_sandbox_workload,
    temporary_volume,
)

MOUNT_PATH = "/sqlite"
PAYLOADS = Path(__file__).parents[1] / "payloads"
WORKER_SOURCE = PAYLOADS / "sqlite" / "sqlite_workload.py"
WORKER_DEST = "/opt/chalk/benchmarks/sqlite-workload.py"
COMMIT_SCRIPT_SOURCE = PAYLOADS / "commit.sh"
COMMIT_SCRIPT_DEST = "/opt/chalk/benchmarks/commit-volume.sh"
MODES = ("write", "verify")

ENTRYPOINT = """
python3 "$1" \
  --mount-path "$2" \
  --mode "$3" \
  --size-mib "$4" \
  --result "$2/$3-result.json" >"$2/$3.log" 2>&1
benchmark_status=$?
commit_started_ns=$(date +%s%N)
/bin/sh "$5" "$2" "$2/$3.log"
commit_status=$?
commit_finished_ns=$(date +%s%N)
printf '{"elapsed_ns":%s}\n' "$((commit_finished_ns - commit_started_ns))" \
  >"$2/$3-commit.json"
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size-mib", type=positive_int, default=5120)
    parser.add_argument("--cpu", default="2")
    parser.add_argument("--memory", default="4Gi")
    parser.add_argument("--timeout-secs", type=positive_int, default=3600)
    parser.add_argument("--keep-volume", action="store_true")
    return parser.parse_args()


def _run_mode(
    mode: str, image: Image, volume: Volume, args: argparse.Namespace
) -> tuple[int, dict[str, Any], float]:
    sandbox = Sandbox(
        image=image,
        cpu=args.cpu,
        memory=args.memory,
        volumes=[volume.mount(MOUNT_PATH)],
        entrypoint=[
            "/bin/sh",
            "-c",
            ENTRYPOINT,
            f"sqlite-{mode}",
            WORKER_DEST,
            MOUNT_PATH,
            mode,
            str(args.size_mib),
            COMMIT_SCRIPT_DEST,
        ],
    )
    run_error: RuntimeError | None = None
    try:
        run_sandbox_workload(
            sandbox,
            phase=f"sqlite {mode}",
            timeout_secs=args.timeout_secs,
            log=(volume, f"{mode}.log"),
        )
    except RuntimeError as error:
        # A failed SQLite phase still commits its partial result
        run_error = error

    try:
        committed = read_committed_json(
            volume,
            {"result": f"{mode}-result.json", "commit": f"{mode}-commit.json"},
            description=f"SQLite {mode} results",
        )
    except TimeoutError:
        if run_error is not None:
            raise run_error
        raise
    if run_error is not None and "error" not in committed.value["result"]:
        raise run_error
    commit_ns = committed.value["commit"]["elapsed_ns"]
    if not isinstance(commit_ns, int) or commit_ns < 0:
        raise ValueError("SQLite commit timing is invalid")
    return committed.version_id, committed.value["result"], commit_ns / 1e9


def build_pyperf_results(
    results: dict[str, tuple[int, dict[str, Any], float]],
    args: argparse.Namespace,
) -> dict[str, Any]:
    common = {
        "workload": "sqlite",
        "sqlite_size_mib": args.size_mib,
        "sandbox_cpu": args.cpu,
        "sandbox_memory": args.memory,
    }
    benchmarks = []
    for mode, (version_id, result, commit_seconds) in results.items():
        runs: dict[str, tuple[list[float], dict[str, Any]]] = {}
        for phase, detail in result["phases"].items():
            values = detail.pop("values", [detail["elapsed_seconds"]])
            runs[f"sqlite-{phase}"] = (
                values,
                {f"sqlite_{key}": value for key, value in detail.items()},
            )
        runs[f"sqlite-{mode}-volume-commit"] = ([commit_seconds], {})
        for name, (values, metadata) in runs.items():
            benchmarks.append(
                {
                    "metadata": {
                        "name": name,
                        "unit": "second",
                        "sqlite_mode": mode,
                        "volume_version_id": version_id,
                        **common,
                        **metadata,
                    },
                    "runs": [{"values": values}],
                }
            )
    return {"version": "1.0", "metadata": common, "benchmarks": benchmarks}


def main() -> dict[str, Any]:
    args = parse_args()
    image = (
        Image("python:3.12-slim")
        .add_local_file(str(WORKER_SOURCE), WORKER_DEST, mode=0o755, strategy="copy")
        .add_local_file(
            str(COMMIT_SCRIPT_SOURCE), COMMIT_SCRIPT_DEST, mode=0o755, strategy="copy"
        )
    )
    with temporary_volume("sandbox-sqlite", keep=args.keep_volume) as volume:
        results: dict[str, tuple[int, dict[str, Any], float]] = {}
        errors: list[str] = []
        for mode in MODES:
            results[mode] = _run_mode(mode, image, volume, args)
            error = results[mode][1].get("error")
            if error:
                errors.append(f"{mode}: {error}")
                break
        pyperf = build_pyperf_results(results, args)
        if errors:
            raise BenchmarkFailure(
                f"SQLite workload failed: {'; '.join(errors)}", pyperf
            )
        return pyperf


if __name__ == "__main__":
    run_benchmark("volume-sqlite", main)
