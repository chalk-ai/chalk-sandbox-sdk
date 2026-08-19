"""Tests for shared ConnectRPC instrumentation."""

from __future__ import annotations

from connectrpc.code import Code
from connectrpc.errors import ConnectError
from connectrpc.method import IdempotencyLevel, MethodInfo
from connectrpc.request import Headers, RequestContext

from chalkcompute import _rpc


def _context() -> RequestContext[object, object]:
    return RequestContext(
        method=MethodInfo(
            name="GetContainer",
            service_name="chalk.server.v1.ContainerService",
            input=object,
            output=object,
            idempotency_level=IdempotencyLevel.UNKNOWN,
        ),
        http_method="POST",
        request_headers=Headers(),
    )


def _recording_interceptor(monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(_rpc, "get_tracer", lambda: provider.get_tracer("test"))
    return _rpc.TracingInterceptor(), exporter


def test_rpc_span_contract(monkeypatch) -> None:
    from opentelemetry.trace import StatusCode

    interceptor, exporter = _recording_interceptor(monkeypatch)
    ctx = _context()
    token = interceptor.on_start_sync(ctx)
    interceptor.on_end_sync(token, ctx, None)

    (span,) = exporter.get_finished_spans()
    assert span.name == "chalkcompute.rpc"
    assert span.attributes["rpc.system.name"] == "grpc"
    assert (
        span.attributes["rpc.method"] == "chalk.server.v1.ContainerService/GetContainer"
    )
    assert span.attributes["rpc.response.status_code"] == "OK"
    assert span.status.status_code is StatusCode.UNSET


def test_rpc_span_marks_failed_calls_as_errors(monkeypatch) -> None:
    from opentelemetry.trace import StatusCode

    interceptor, exporter = _recording_interceptor(monkeypatch)
    ctx = _context()
    token = interceptor.on_start_sync(ctx)
    interceptor.on_end_sync(token, ctx, ConnectError(Code.UNAVAILABLE, "offline"))

    (span,) = exporter.get_finished_spans()
    assert span.attributes["rpc.system.name"] == "grpc"
    assert span.attributes["rpc.response.status_code"] == "UNAVAILABLE"
    assert span.status.status_code is StatusCode.ERROR
