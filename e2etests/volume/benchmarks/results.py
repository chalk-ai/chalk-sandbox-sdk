from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any, Callable

from chalkcompute import SandboxCreationError

from e2etests.volume.benchmarks.runtime import VolumeSpoolExhaustedError

HOST_CAPACITY_ERROR = "all registered host-pool capacity is allocated"


def build_status_benchmark(
    *, request_type: str, error_count: int, error_type: str | None = None
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "name": "benchmark-status",
        "benchmark_error_count": error_count,
        "request_type": request_type,
    }
    if error_type is not None:
        metadata["error_type"] = error_type
    return {"metadata": metadata, "runs": [{"values": []}]}


def record_status(
    output: Path,
    *,
    request_type: str,
    error_count: int,
    error_type: str | None = None,
    require_results: bool = False,
) -> None:
    if output.exists() and output.stat().st_size > 0:
        result = json.loads(output.read_text())
    elif require_results:
        raise RuntimeError(f"Benchmark produced no output at {output}")
    else:
        result = {"version": "1.0", "benchmarks": []}
    benchmarks = result.get("benchmarks")
    if not isinstance(benchmarks, list):
        raise ValueError("Benchmark output has no benchmarks list")
    benchmarks.append(
        build_status_benchmark(
            request_type=request_type,
            error_count=error_count,
            error_type=error_type,
        )
    )
    output.write_text(json.dumps(result, sort_keys=True))


def run_benchmark(request_type: str, benchmark: Callable[[], None]) -> None:
    output = os.environ.get("CHALK_BENCHMARK_PYPERF_OUTPUT")
    if not output:
        raise RuntimeError("CHALK_BENCHMARK_PYPERF_OUTPUT must be set")
    output_path = Path(output)
    try:
        benchmark()
    except SandboxCreationError as error:
        if HOST_CAPACITY_ERROR not in str(error):
            raise
        print(error, file=sys.stderr)
        record_status(
            output_path,
            request_type=request_type,
            error_count=1,
            error_type="sandbox-capacity-exhausted",
        )
    except VolumeSpoolExhaustedError:
        record_status(
            output_path,
            request_type=request_type,
            error_count=1,
            error_type="volume-spool-exhausted",
        )
        raise
    else:
        record_status(
            output_path,
            request_type=request_type,
            error_count=0,
            require_results=True,
        )
