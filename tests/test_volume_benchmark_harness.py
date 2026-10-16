from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, NoReturn, cast

import pytest

from chalkcompute import SandboxCreationError
from e2etests.volume.benchmarks import harness
from e2etests.volume.benchmarks.harness import (
    BENCHMARK_RESULTS_PREFIX,
    BENCHMARK_RESULTS_VOLUME,
    BenchmarkFailure,
    HOST_CAPACITY_ERROR,
    SANDBOX_PREEMPTED_MESSAGE,
    SandboxPreemptedError,
    VolumeSpoolExhaustedError,
    archive_benchmark_results,
    read_committed_json,
    run_benchmark,
    run_sandbox_workload,
    temporary_volume,
)


@pytest.fixture(autouse=True)
def stub_results_archive(monkeypatch) -> None:
    monkeypatch.setattr(harness, "archive_benchmark_results", lambda *_args: "")


def test_read_committed_json_retries_and_pins_one_version() -> None:
    class Version:
        def __init__(self, version_id: int) -> None:
            self.id = version_id

    class Snapshot:
        def __init__(self, files: dict[str, bytes]) -> None:
            self.files = files

        def read_file(self, path: str) -> bytes:
            return self.files[path]

    class FakeVolume:
        def __init__(self) -> None:
            self.version_ids = iter((10, 11))
            self.pinned_versions: list[int] = []

        def latest_version(self) -> Version:
            return Version(next(self.version_ids))

        def at_version(self, version_id: int) -> Snapshot:
            self.pinned_versions.append(version_id)
            if version_id == 10:
                return Snapshot({"first.json": b"{"})
            return Snapshot(
                {"first.json": b'{"value":1}', "second.json": b'{"value":2}'}
            )

    volume = FakeVolume()
    committed = read_committed_json(
        cast(Any, volume),
        {"first": "first.json", "second": "second.json"},
        poll_interval=0,
    )

    assert committed.version_id == 11
    assert committed.value == {"first": {"value": 1}, "second": {"value": 2}}
    assert committed.read_attempts == 2
    assert volume.pinned_versions == [10, 11]


def test_temporary_volume_creates_deletes_and_closes(monkeypatch) -> None:
    events: list[tuple[str, str]] = []

    class FakeVolume:
        def __init__(self, name: str) -> None:
            self.name = name

        def reload(self) -> None:
            events.append(("reload", self.name))

        def delete(self) -> None:
            events.append(("delete", self.name))

        def close(self) -> None:
            events.append(("close", self.name))

    monkeypatch.setattr(harness, "Volume", FakeVolume)

    with temporary_volume("benchmark-test") as volume:
        assert volume.name.startswith("benchmark-test-")
        name = volume.name

    assert events == [("reload", name), ("delete", name), ("close", name)]


@pytest.mark.parametrize(
    ("name", "keep"),
    (("existing-volume", False), (None, True)),
)
def test_temporary_volume_preserves_requested_volumes(
    monkeypatch, name: str | None, keep: bool
) -> None:
    deleted: list[str] = []
    closed: list[str] = []

    class FakeVolume:
        def __init__(self, volume_name: str) -> None:
            self.name = volume_name

        def reload(self) -> None:
            pass

        def delete(self) -> None:
            deleted.append(self.name)

        def close(self) -> None:
            closed.append(self.name)

    monkeypatch.setattr(harness, "Volume", FakeVolume)

    with temporary_volume("benchmark-test", name=name, keep=keep) as volume:
        volume_name = volume.name

    assert deleted == []
    assert closed == [volume_name]


def test_run_sandbox_workload_starts_waits_and_terminates(monkeypatch) -> None:
    events: list[tuple[str, object]] = []

    class FakeSandbox:
        id: str | None = None

        def run(self, *, timeout: int) -> None:
            self.id = "sandbox-id"
            events.append(("run", timeout))

        def terminate(self) -> None:
            events.append(("terminate", self.id))
            self.id = None

    def wait(sandbox, timeout_secs: int, phase: str, **kwargs) -> None:
        events.append(("wait", (sandbox.id, timeout_secs, phase, kwargs)))

    monkeypatch.setattr(harness, "_wait_for_sandbox", wait)
    sandbox = FakeSandbox()

    run_sandbox_workload(
        cast(Any, sandbox),
        phase="test phase",
        timeout_secs=30,
        startup_timeout_secs=10,
    )

    assert events == [
        ("run", 10),
        (
            "wait",
            (
                "sandbox-id",
                30,
                "test phase",
                {"log_volume": None, "log_path": None},
            ),
        ),
        ("terminate", "sandbox-id"),
    ]


def test_run_sandbox_workload_terminates_after_wait_failure(monkeypatch) -> None:
    terminated: list[str | None] = []

    class FakeSandbox:
        id: str | None = None

        def run(self, *, timeout: int) -> None:
            self.id = "sandbox-id"

        def terminate(self) -> None:
            terminated.append(self.id)

    def fail(*_args, **_kwargs) -> None:
        raise RuntimeError("workload failed")

    monkeypatch.setattr(harness, "_wait_for_sandbox", fail)
    sandbox = FakeSandbox()

    with pytest.raises(RuntimeError, match="workload failed"):
        run_sandbox_workload(cast(Any, sandbox), phase="test phase", timeout_secs=30)

    assert terminated == ["sandbox-id"]


def test_wait_for_sandbox_classifies_preemption(monkeypatch) -> None:
    monkeypatch.setattr(
        harness,
        "wait_for_completion",
        lambda *_args: ("failed", SANDBOX_PREEMPTED_MESSAGE),
    )

    with pytest.raises(SandboxPreemptedError, match=SANDBOX_PREEMPTED_MESSAGE):
        harness._wait_for_sandbox(cast(Any, object()), 30, "test phase")


def test_run_sandbox_workload_terminates_after_startup_failure() -> None:
    terminated: list[str | None] = []

    class FakeSandbox:
        id: str | None = None

        def run(self, *, timeout: int) -> None:
            self.id = "sandbox-id"
            raise TimeoutError("startup failed")

        def terminate(self) -> None:
            terminated.append(self.id)

    sandbox = FakeSandbox()

    with pytest.raises(TimeoutError, match="startup failed"):
        run_sandbox_workload(cast(Any, sandbox), phase="test phase", timeout_secs=30)

    assert terminated == ["sandbox-id"]


def test_archive_benchmark_results_uses_timestamped_path(tmp_path, monkeypatch) -> None:
    output = tmp_path / "benchmark.json"
    output.write_text('{"version":"1.0"}')
    uploads: list[tuple[str, str, Path]] = []

    class FakeVolume:
        def __init__(self, name: str) -> None:
            self.name = name

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def put_file_from_path(self, path: str, local_path: Path) -> None:
            uploads.append((self.name, path, local_path))

    monkeypatch.setattr(harness, "Volume", FakeVolume)

    path = archive_benchmark_results(output, "volume-test")

    assert uploads == [(BENCHMARK_RESULTS_VOLUME, path, output)]
    assert re.fullmatch(
        rf"{BENCHMARK_RESULTS_PREFIX}/\d{{4}}/\d{{2}}/\d{{2}}/"
        r"\d{8}T\d{6}\.\d{6}Z-volume-test\.json",
        path,
    )


def test_run_benchmark_writes_and_archives_returned_results(
    tmp_path, monkeypatch
) -> None:
    output = tmp_path / "benchmark.json"
    monkeypatch.setenv("CHALK_BENCHMARK_PYPERF_OUTPUT", str(output))
    archived: list[dict[str, Any]] = []
    monkeypatch.setattr(
        harness,
        "archive_benchmark_results",
        lambda path, _request_type: archived.append(json.loads(path.read_text())),
    )

    def benchmark() -> dict[str, Any]:
        return {"version": "1.0", "benchmarks": []}

    run_benchmark("volume-test", benchmark)

    result = json.loads(output.read_text())
    assert archived == [result]
    assert result["benchmarks"][0]["metadata"]["benchmark_error_count"] == 0


def test_run_benchmark_preserves_results_from_failure(tmp_path, monkeypatch) -> None:
    output = tmp_path / "benchmark.json"
    monkeypatch.setenv("CHALK_BENCHMARK_PYPERF_OUTPUT", str(output))
    archived: list[Path] = []
    monkeypatch.setattr(
        harness,
        "archive_benchmark_results",
        lambda path, _request_type: archived.append(path),
    )
    results = {"version": "1.0", "benchmarks": [{"runs": []}]}

    def benchmark() -> NoReturn:
        raise BenchmarkFailure("benchmark failed", results)

    with pytest.raises(BenchmarkFailure, match="benchmark failed"):
        run_benchmark("volume-test", benchmark)

    assert json.loads(output.read_text()) == results
    assert archived == [output]


@pytest.mark.parametrize(
    "message",
    (
        HOST_CAPACITY_ERROR,
        "Unschedulable: no eligible registered host has available capacity "
        "(requested cpu=2000m, memory=4294967296 bytes)",
    ),
)
def test_run_benchmark_records_suppressed_known_error(
    tmp_path, monkeypatch, message: str
) -> None:
    output = tmp_path / "benchmark.json"
    monkeypatch.setenv("CHALK_BENCHMARK_PYPERF_OUTPUT", str(output))

    def benchmark() -> NoReturn:
        raise SandboxCreationError(f"Unable to schedule: {message}")

    run_benchmark("volume-test", benchmark)

    metadata = json.loads(output.read_text())["benchmarks"][0]["metadata"]
    assert metadata["benchmark_error_count"] == 1
    assert metadata["error_type"] == "sandbox-capacity-exhausted"


def test_run_benchmark_reports_sandbox_preemption(tmp_path, monkeypatch) -> None:
    output = tmp_path / "benchmark.json"
    monkeypatch.setenv("CHALK_BENCHMARK_PYPERF_OUTPUT", str(output))

    def benchmark() -> NoReturn:
        raise SandboxPreemptedError(SANDBOX_PREEMPTED_MESSAGE)

    run_benchmark("volume-test", benchmark)

    metadata = json.loads(output.read_text())["benchmarks"][0]["metadata"]
    assert metadata["benchmark_error_count"] == 1
    assert metadata["error_type"] == "sandbox-preempted"


@pytest.mark.parametrize(
    "status_message",
    (
        "Container marked terminated because its host became unavailable during a maintenance window.",
        "Container marked terminated because its host became unavailable.",
    ),
)
def test_run_benchmark_reports_host_unavailability(
    tmp_path, monkeypatch, status_message: str
) -> None:
    output = tmp_path / "benchmark.json"
    monkeypatch.setattr(
        harness,
        "wait_for_completion",
        lambda *_args: ("terminated", status_message),
    )

    def benchmark() -> NoReturn:
        harness._wait_for_sandbox(cast(Any, object()), 30, "fio")
        pytest.fail("Expected host unavailability")

    run_benchmark("volume-fio", benchmark, output=output)

    metadata = json.loads(output.read_text())["benchmarks"][0]["metadata"]
    assert metadata["benchmark_error_count"] == 1
    assert metadata["error_type"] == "sandbox-host-unavailable"
    assert metadata["request_type"] == "volume-fio"


@pytest.mark.parametrize("status_message", (None, "Terminated by user"))
def test_run_benchmark_reraises_other_terminations(
    tmp_path, monkeypatch, status_message: str | None
) -> None:
    output = tmp_path / "benchmark.json"
    monkeypatch.setattr(
        harness,
        "wait_for_completion",
        lambda *_args: ("terminated", status_message),
    )

    def benchmark() -> NoReturn:
        harness._wait_for_sandbox(cast(Any, object()), 30, "fio")
        pytest.fail("Expected termination")

    with pytest.raises(
        RuntimeError, match="fio sandbox completed with status terminated"
    ):
        run_benchmark("volume-fio", benchmark, output=output)

    metadata = json.loads(output.read_text())["benchmarks"][0]["metadata"]
    assert metadata["benchmark_error_count"] == 1
    assert metadata["error_type"] == "unclassified"
    assert metadata["exception_class"] == "RuntimeError"
    assert metadata["request_type"] == "volume-fio"


def test_run_benchmark_records_and_reraises_known_error(tmp_path, monkeypatch) -> None:
    output = tmp_path / "benchmark.json"
    monkeypatch.setenv("CHALK_BENCHMARK_PYPERF_OUTPUT", str(output))

    def benchmark() -> NoReturn:
        raise VolumeSpoolExhaustedError("spool exhausted")

    with pytest.raises(VolumeSpoolExhaustedError, match="spool exhausted"):
        run_benchmark("volume-test", benchmark)

    metadata = json.loads(output.read_text())["benchmarks"][0]["metadata"]
    assert metadata["benchmark_error_count"] == 1
    assert metadata["error_type"] == "volume-spool-exhausted"


def test_run_benchmark_reraises_unknown_error(tmp_path, monkeypatch) -> None:
    output = tmp_path / "benchmark.json"
    monkeypatch.setenv("CHALK_BENCHMARK_PYPERF_OUTPUT", str(output))

    def benchmark() -> NoReturn:
        raise SandboxCreationError("Unexpected creation failure")

    with pytest.raises(SandboxCreationError, match="Unexpected creation failure"):
        run_benchmark("volume-test", benchmark)

    metadata = json.loads(output.read_text())["benchmarks"][0]["metadata"]
    assert metadata["benchmark_error_count"] == 1
    assert metadata["error_type"] == "unclassified"
    assert metadata["exception_class"] == "SandboxCreationError"
    assert metadata["request_type"] == "volume-test"
