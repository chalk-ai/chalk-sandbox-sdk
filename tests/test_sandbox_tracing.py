from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from protobuf.wkt import Timestamp

from chalkcompute import Sandbox, _tracing
from chalkcompute._gen.chalk.sandbox.v2 import service_pb as pb
from chalkcompute._containerspec import ComputeClass
from chalkcompute._sandbox import SandboxClient, SandboxCreationError, SandboxInfo


@pytest.fixture
def recorded_spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(_tracing, "get_tracer", lambda: provider.get_tracer("test"))
    monkeypatch.setattr(_tracing, "_has_tracing_intent", lambda: True)
    monkeypatch.setattr(_tracing, "_is_remote_tracing_off", lambda: False)
    try:
        yield exporter
    finally:
        provider.shutdown()


class _SandboxStub:
    def __init__(self, status: pb.SandboxStatus) -> None:
        super().__init__()
        self.status = status

    def create_sandbox(self, request, **kwargs):
        return pb.CreateSandboxResponse(
            sandbox=pb.SandboxInfo(
                id="sandbox-traced",
                status=self.status,
                created_at=Timestamp(seconds=1),
            )
        )

    def get_sandbox(self, request, **kwargs):
        return pb.GetSandboxResponse(
            sandbox=pb.SandboxInfo(
                id="sandbox-traced",
                status=pb.SandboxStatus.FAILED,
                status_message="readiness failed",
                created_at=Timestamp(seconds=1),
            )
        )

    def terminate_sandbox(self, request, **kwargs):
        return pb.TerminateSandboxResponse()


def test_lifecycle_spans_keep_sandbox_id_after_termination(recorded_spans):
    with SandboxClient() as client:
        client._stub = _SandboxStub(pb.SandboxStatus.RUNNING)
        sandbox = Sandbox(image="ubuntu:latest", sandbox_client=client).run(wait=False)
        sandbox.terminate()
        assert sandbox.id is None

    spans = {span.name: span for span in recorded_spans.get_finished_spans()}
    for operation in ("run", "create", "terminate"):
        assert (
            spans[f"chalkcompute.sandbox.{operation}"].attributes["sandbox.id"]
            == "sandbox-traced"
        )


def test_creation_span_keeps_identity_when_readiness_fails(recorded_spans):
    with SandboxClient() as client:
        client._stub = _SandboxStub(pb.SandboxStatus.PENDING)
        sandbox = Sandbox(image="ubuntu:latest", sandbox_client=client)
        with pytest.raises(SandboxCreationError):
            sandbox.run(poll_interval=0, timeout=1)
        assert sandbox.id is None

    span = next(
        span
        for span in recorded_spans.get_finished_spans()
        if span.name == "chalkcompute.sandbox.create"
    )
    assert span.attributes["sandbox.id"] == "sandbox-traced"
    assert span.status.status_code is StatusCode.ERROR


def test_exec_failure_keeps_the_target_sandbox_id(recorded_spans, monkeypatch):
    sandbox = Sandbox(sandbox_id="sandbox-exec")
    sandbox._info = SandboxInfo(
        id="sandbox-exec",
        status="running",
        created_at="",
        compute_class=ComputeClass.HOST,
    )
    process = MagicMock()
    process.wait.side_effect = RuntimeError("process session failed")
    monkeypatch.setattr(sandbox, "exec_start", lambda *args, **kwargs: process)
    with pytest.raises(RuntimeError, match="process session failed"):
        sandbox.exec("false")
    (span,) = recorded_spans.get_finished_spans()
    assert span.attributes["sandbox.id"] == "sandbox-exec"
    assert span.status.status_code is StatusCode.ERROR


def test_disabled_tracing_does_not_stamp_the_callers_span(recorded_spans, monkeypatch):
    from opentelemetry.trace import get_current_span

    with _tracing.get_tracer().start_as_current_span("caller") as caller:
        monkeypatch.setattr(_tracing, "_is_remote_tracing_off", lambda: True)
        _tracing._set_sandbox_span_id("sandbox-disabled")
        assert get_current_span() is caller
        assert "sandbox.id" not in caller.attributes


def test_python_execution_records_identity_before_authentication_failure(
    recorded_spans, monkeypatch
):
    sandbox = Sandbox(sandbox_id="sandbox-python")

    def unavailable_client():
        raise RuntimeError("authentication unavailable")

    monkeypatch.setattr(sandbox, "_require_client", unavailable_client)
    with pytest.raises(RuntimeError, match="authentication unavailable"):
        sandbox.execute_python("1 + 1")
    (span,) = recorded_spans.get_finished_spans()
    assert span.name == "chalkcompute.sandbox.execute_python"
    assert span.attributes["sandbox.id"] == "sandbox-python"
    assert span.status.status_code is StatusCode.ERROR
