from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Generic, Iterator, Mapping, TypeVar
import uuid

from chalkcompute import (
    Sandbox,
    SandboxCreationError,
    SandboxNotFoundError,
    Volume,
    VolumeError,
)

HOST_CAPACITY_ERROR = "all registered host-pool capacity is allocated"
SANDBOX_PREEMPTED_MESSAGE = "container was preempted"
SANDBOX_HOST_UNAVAILABLE_MESSAGE = (
    "Container marked terminated because its host became unavailable"
)
BENCHMARK_RESULTS_VOLUME = "volume-bench-results"
BENCHMARK_RESULTS_PREFIX = "pyperf"
VOLUME_SPOOL_EXHAUSTED_EXIT_CODE = 75
VOLUME_SPOOL_EXHAUSTED_MESSAGE = "Chalk volume write spool exhausted"
T = TypeVar("T")


class VolumeSpoolExhaustedError(RuntimeError):
    pass


class SandboxPreemptedError(RuntimeError):
    pass


class SandboxHostUnavailableError(RuntimeError):
    pass


class BenchmarkFailure(RuntimeError):
    def __init__(self, message: str, results: dict[str, Any]) -> None:
        super().__init__(message)
        self.results = results


@dataclass(frozen=True)
class CommittedVolumeResult(Generic[T]):
    version_id: int
    value: T
    ready_seconds: float
    read_attempts: int


@dataclass(frozen=True)
class KnownError:
    error_type: str
    exception_type: type[Exception]
    message_contains: str | None = None
    reraise: bool = False

    def matches(self, error: Exception) -> bool:
        return isinstance(error, self.exception_type) and (
            self.message_contains is None or self.message_contains in str(error)
        )


KNOWN_ERRORS = (
    KnownError(
        error_type="sandbox-capacity-exhausted",
        exception_type=SandboxCreationError,
        message_contains=HOST_CAPACITY_ERROR,
    ),
    KnownError(
        error_type="sandbox-preempted",
        exception_type=SandboxPreemptedError,
    ),
    KnownError(
        error_type="sandbox-host-unavailable",
        exception_type=SandboxHostUnavailableError,
    ),
    KnownError(
        error_type="volume-spool-exhausted",
        exception_type=VolumeSpoolExhaustedError,
        reraise=True,
    ),
)


def find_known_error(error: Exception) -> KnownError | None:
    return next((known for known in KNOWN_ERRORS if known.matches(error)), None)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def terminate_sandbox(sandbox: Sandbox) -> None:
    try:
        sandbox.terminate()
    except SandboxNotFoundError:
        pass


def delete_volume(volume: Volume) -> None:
    try:
        volume.delete()
    except VolumeError as error:
        print(f"Unable to delete volume {volume.name}: {error}", file=sys.stderr)


@contextmanager
def temporary_volume(
    prefix: str,
    *,
    name: str | None = None,
    keep: bool = False,
) -> Iterator[Volume]:
    generated = name is None
    volume = Volume(name or f"{prefix}-{uuid.uuid4().hex[:12]}")
    try:
        volume.reload()
        yield volume
    finally:
        if generated and not keep:
            delete_volume(volume)
        volume.close()


def wait_for_completion(
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


def print_volume_log(volume: Volume, path: str, timeout_secs: int = 15) -> None:
    deadline = time.monotonic() + timeout_secs
    while True:
        try:
            version_id = volume.latest_version().id
            data = volume.at_version(version_id).read_file(path)
            break
        except VolumeError as error:
            if time.monotonic() >= deadline:
                print(f"Unable to read sandbox log {path!r}: {error}", file=sys.stderr)
                return
            time.sleep(1)
    log = data.decode(errors="replace")
    print(log, end="" if log.endswith("\n") else "\n", file=sys.stderr, flush=True)


def read_committed_volume(
    volume: Volume,
    reader: Callable[[Volume], T],
    *,
    timeout_secs: int = 120,
    poll_interval: float = 2,
    retry_errors: tuple[type[Exception], ...] = (VolumeError,),
    description: str = "Volume results",
) -> CommittedVolumeResult[T]:
    started_at = time.monotonic()
    deadline = started_at + timeout_secs
    attempts = 0
    version_id: int | None = None
    while True:
        attempts += 1
        try:
            version_id = volume.latest_version().id
            snapshot = volume.at_version(version_id)
            value = reader(snapshot)
            return CommittedVolumeResult(
                version_id=version_id,
                value=value,
                ready_seconds=time.monotonic() - started_at,
                read_attempts=attempts,
            )
        except retry_errors as error:
            if time.monotonic() >= deadline:
                version_detail = (
                    f"latest version {version_id}"
                    if version_id is not None
                    else "no version"
                )
                raise TimeoutError(
                    f"{description} did not become readable within {timeout_secs} "
                    + f"seconds ({version_detail}): {error}"
                ) from error
            time.sleep(poll_interval)


def read_committed_json(
    volume: Volume,
    paths: Mapping[str, str],
    *,
    timeout_secs: int = 120,
    poll_interval: float = 2,
    description: str = "Volume JSON results",
) -> CommittedVolumeResult[dict[str, dict[str, Any]]]:
    def read(snapshot: Volume) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for name, path in paths.items():
            value = json.loads(snapshot.read_file(path))
            if not isinstance(value, dict):
                raise ValueError(f"Volume result {path!r} is not an object")
            results[name] = value
        return results

    return read_committed_volume(
        volume,
        read,
        timeout_secs=timeout_secs,
        poll_interval=poll_interval,
        retry_errors=(VolumeError, ValueError),
        description=description,
    )


def _wait_for_sandbox(
    sandbox: Sandbox,
    timeout_secs: int,
    phase: str,
    *,
    log_volume: Volume | None = None,
    log_path: str | None = None,
) -> None:
    status, status_message = wait_for_completion(sandbox, timeout_secs)
    if status == "succeeded":
        return
    if log_volume is not None and log_path is not None:
        print_volume_log(log_volume, log_path)
    if status_message == f"Exit code {VOLUME_SPOOL_EXHAUSTED_EXIT_CODE}":
        raise VolumeSpoolExhaustedError(
            f"{phase} sandbox completed with status {status}: "
            f"{VOLUME_SPOOL_EXHAUSTED_MESSAGE}"
        )
    if status_message == SANDBOX_PREEMPTED_MESSAGE:
        raise SandboxPreemptedError(
            f"{phase} sandbox completed with status {status}: {status_message}"
        )
    if status_message and status_message.startswith(SANDBOX_HOST_UNAVAILABLE_MESSAGE):
        raise SandboxHostUnavailableError(
            f"{phase} sandbox completed with status {status}: {status_message}"
        )
    detail = f": {status_message}" if status_message else ""
    raise RuntimeError(f"{phase} sandbox completed with status {status}{detail}")


def run_sandbox_workload(
    sandbox: Sandbox,
    *,
    phase: str,
    timeout_secs: int,
    startup_timeout_secs: int = 600,
    log: tuple[Volume, str] | None = None,
) -> None:
    try:
        sandbox.run(timeout=startup_timeout_secs)
        print(f"Running {phase} in sandbox {sandbox.id}", flush=True)
        _wait_for_sandbox(
            sandbox,
            timeout_secs,
            phase,
            log_volume=log[0] if log is not None else None,
            log_path=log[1] if log is not None else None,
        )
    finally:
        if sandbox.id is not None:
            terminate_sandbox(sandbox)


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


def append_status(
    results: dict[str, Any],
    *,
    request_type: str,
    error_count: int,
    error_type: str | None = None,
) -> None:
    benchmarks = results.get("benchmarks")
    if not isinstance(benchmarks, list):
        raise ValueError("Benchmark results have no benchmarks list")
    benchmarks.append(
        build_status_benchmark(
            request_type=request_type,
            error_count=error_count,
            error_type=error_type,
        )
    )


def archive_benchmark_results(output: Path, request_type: str) -> str:
    timestamp = datetime.now(UTC)
    date_prefix = timestamp.strftime("%Y/%m/%d")
    filename = f"{timestamp.strftime('%Y%m%dT%H%M%S.%fZ')}-{request_type}.json"
    path = f"{BENCHMARK_RESULTS_PREFIX}/{date_prefix}/{filename}"
    with Volume(BENCHMARK_RESULTS_VOLUME) as volume:
        volume.put_file_from_path(path, output)
    print(
        f"Archived benchmark results to {BENCHMARK_RESULTS_VOLUME}/{path}", flush=True
    )
    return path


def publish_benchmark_results(
    output: Path,
    request_type: str,
    results: dict[str, Any],
    *,
    error_count: int | None = None,
    error_type: str | None = None,
    archive: bool = True,
) -> None:
    if error_count is not None:
        append_status(
            results,
            request_type=request_type,
            error_count=error_count,
            error_type=error_type,
        )
    output.write_text(json.dumps(results, sort_keys=True))
    if archive:
        archive_benchmark_results(output, request_type)


def run_benchmark(
    request_type: str,
    benchmark: Callable[[], dict[str, Any]],
    *,
    output: Path | None = None,
    archive: bool = True,
) -> None:
    output_value = output or os.environ.get("CHALK_BENCHMARK_PYPERF_OUTPUT")
    if not output_value:
        raise RuntimeError("CHALK_BENCHMARK_PYPERF_OUTPUT must be set")
    output_path = Path(output_value)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        results = benchmark()
    except BenchmarkFailure as error:
        publish_benchmark_results(
            output_path, request_type, error.results, archive=archive
        )
        raise
    except Exception as error:
        known_error = find_known_error(error)
        if known_error is None:
            raise
        if not known_error.reraise:
            print(error, file=sys.stderr)
        publish_benchmark_results(
            output_path,
            request_type,
            {"version": "1.0", "benchmarks": []},
            error_count=1,
            error_type=known_error.error_type,
            archive=archive,
        )
        if known_error.reraise:
            raise
    else:
        publish_benchmark_results(
            output_path,
            request_type,
            results,
            error_count=0,
            archive=archive,
        )
