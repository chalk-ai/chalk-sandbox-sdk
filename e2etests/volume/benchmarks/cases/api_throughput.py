"""Benchmark Volume API throughput through the Python SDK."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
import logging
import statistics
import sys
import time
from typing import Any, Callable, Iterable, Iterator, TypeVar
import uuid

from chalkcompute import VolumeClient, VolumeError, VolumeNotFoundError
from e2etests.volume.benchmarks.harness import (
    BenchmarkFailure,
    positive_int,
    run_benchmark,
    temporary_volume,
)

T = TypeVar("T")
BATCH_FILES = 200


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--volumes", type=positive_int, default=10_000)
    parser.add_argument("--files", type=positive_int, default=5_000)
    parser.add_argument("--file-bytes", type=int, default=1)
    parser.add_argument("--concurrency", type=positive_int, default=32)
    parser.add_argument("--max-error-rate", type=float, default=0.05)
    return parser.parse_args()


@dataclass
class Phase:
    elapsed: float
    latencies: list[float]
    failed: list[Any]


def timed_map(
    name: str, fn: Callable[[T], Any], items: Iterable[T], concurrency: int
) -> Phase:
    def run(item: T) -> float | Exception:
        started = time.perf_counter()
        try:
            fn(item)
        except Exception as error:
            return error
        return time.perf_counter() - started

    items = list(items)
    started = time.perf_counter()
    with ThreadPoolExecutor(concurrency) as pool:
        outcomes = list(pool.map(run, items))
    elapsed = time.perf_counter() - started
    failed = [
        (item, outcome)
        for item, outcome in zip(items, outcomes)
        if isinstance(outcome, Exception)
    ]
    if failed:
        print(
            f"{name}: {len(failed)} of {len(items)} requests failed, "
            f"first error: {failed[0][1]!r}",
            file=sys.stderr,
        )
    latencies = [outcome for outcome in outcomes if isinstance(outcome, float)]
    return Phase(elapsed, latencies, [item for item, _ in failed])


def throughput_benchmark(name: str, phase: Phase, **metadata: Any) -> dict[str, Any]:
    ok = len(phase.latencies)
    ops = ok + len(phase.failed)
    latency: dict[str, float] = {}
    if ok:
        # Percentiles need two samples so a single op reports its own latency
        cuts = (
            statistics.quantiles(phase.latencies, n=100)
            if ok > 1
            else phase.latencies * 99
        )
        latency = {
            "api_latency_mean_sec": statistics.fmean(phase.latencies),
            "api_latency_p50_sec": cuts[49],
            "api_latency_p95_sec": cuts[94],
            "api_latency_p99_sec": cuts[98],
        }
    return {
        "metadata": {
            "name": name,
            "unit": "second",
            "api_ops": ops,
            "api_errors": len(phase.failed),
            "api_error_rate": len(phase.failed) / ops if ops else 0.0,
            "api_ops_per_sec": ok / phase.elapsed if phase.elapsed else 0.0,
            **latency,
            **metadata,
        },
        "runs": [{"values": [phase.elapsed]}],
    }


class FirstAttemptFailures(logging.Handler):
    """Count requests the SDK retried after a failed first attempt."""

    def __init__(self) -> None:
        super().__init__()
        self.by_method: Counter[str] = Counter()

    def emit(self, record: logging.LogRecord) -> None:
        # ponytail: parses RetryInterceptor log args, use an SDK retry hook if one lands
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and args[2] == 1:
            self.by_method[str(args[1])] += 1


@contextmanager
def count_first_attempt_failures() -> Iterator[FirstAttemptFailures]:
    logger = logging.getLogger("chalkcompute._rpc")
    handler = FirstAttemptFailures()
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


def first_attempt_metadata(failures: int, phase: Phase) -> dict[str, Any]:
    ops = len(phase.latencies) + len(phase.failed)
    return {
        "api_first_attempt_errors": failures,
        "api_first_attempt_error_rate": failures / ops if ops else 0.0,
    }


def bench_volumes(
    client: VolumeClient, count: int, concurrency: int
) -> list[dict[str, Any]]:
    prefix = f"bench-api-{uuid.uuid4().hex[:12]}"
    names = [f"{prefix}-{i:05d}" for i in range(count)]
    created: set[str] = set()
    # Failed creates may still exist server side, such as after a timeout
    unconfirmed: set[str] = set()

    def create(name: str) -> None:
        unconfirmed.add(name)
        client.create(name)
        created.add(name)
        unconfirmed.discard(name)

    def delete(name: str) -> None:
        client.delete_volume(name)
        created.discard(name)

    try:
        with count_first_attempt_failures() as retried:
            create_phase = timed_map("create-volumes", create, names, concurrency)
            delete_phase = timed_map(
                "delete-volumes", delete, sorted(created), concurrency
            )
    finally:
        for name in sorted(created | unconfirmed):
            try:
                client.delete_volume(name)
            except VolumeNotFoundError:
                pass
            except VolumeError as error:
                print(f"Unable to delete volume {name}: {error}", file=sys.stderr)
    return [
        throughput_benchmark(
            "create-volumes",
            create_phase,
            **first_attempt_metadata(retried.by_method["CreateVolume"], create_phase),
        ),
        throughput_benchmark(
            "delete-volumes",
            delete_phase,
            **first_attempt_metadata(retried.by_method["DeleteVolume"], delete_phase),
        ),
    ]


def bench_files(count: int, file_bytes: int, concurrency: int) -> list[dict[str, Any]]:
    data = b"x" * file_bytes
    paths = [f"files/{i:05d}" for i in range(count)]
    batches = [paths[i : i + BATCH_FILES] for i in range(0, count, BATCH_FILES)]
    with temporary_volume("bench-api-files") as volume:

        def upload(batch_paths: list[str]) -> None:
            with volume.batch_upload(max_files_per_commit=BATCH_FILES) as batch:
                for path in batch_paths:
                    batch.put(path, data)

        # One commit at a time since concurrent commits to one ref conflict
        upload_phase = timed_map("upload-files", upload, batches, 1)
        failed = {path for batch_paths in upload_phase.failed for path in batch_paths}
        uploaded = [path for path in paths if path not in failed]
        version_id = volume.version.id if uploaded else None
        snapshot = volume.at_version(version_id) if version_id is not None else volume

        def read(path: str) -> None:
            if snapshot.read_file(path) != data:
                raise VolumeError(f"Unexpected contents for {path!r}")

        read_phase = timed_map("read-files", read, uploaded, concurrency)
    common = {"file_bytes": file_bytes, "volume_version_id": version_id}
    return [
        throughput_benchmark(
            "upload-files", upload_phase, files_per_commit=BATCH_FILES, **common
        ),
        throughput_benchmark("read-files", read_phase, **common),
    ]


def main() -> dict[str, Any]:
    args = parse_args()
    with VolumeClient.from_env() as client:
        benchmarks = bench_volumes(client, args.volumes, args.concurrency)
    benchmarks += bench_files(args.files, args.file_bytes, args.concurrency)
    results = {
        "version": "1.0",
        "metadata": {
            "workload": "volume-api-throughput",
            "api_concurrency": args.concurrency,
            "api_max_error_rate": args.max_error_rate,
        },
        "benchmarks": benchmarks,
    }
    check_error_rate(results, args.max_error_rate)
    return results


def check_error_rate(results: dict[str, Any], max_error_rate: float) -> None:
    failing = [
        f"{b['metadata']['name']} {b['metadata']['api_error_rate']:.1%}"
        for b in results["benchmarks"]
        if b["metadata"]["api_error_rate"] > max_error_rate
    ]
    if failing:
        raise BenchmarkFailure(
            f"API error rate above {max_error_rate:.0%}: {', '.join(failing)}",
            results,
        )


if __name__ == "__main__":
    run_benchmark("volume-api-throughput", main)
