"""Tests for shared ConnectRPC instrumentation."""

from __future__ import annotations

import pytest
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
    assert span.attributes["rpc.response.status_code"] == "UNAVAILABLE"
    assert span.status.status_code is StatusCode.ERROR


def test_rpc_interceptors_include_retry_and_tracing() -> None:
    interceptors = _rpc.rpc_interceptors()
    assert len(interceptors) == 2
    assert isinstance(interceptors[0], _rpc.RetryInterceptor)
    assert isinstance(interceptors[1], _rpc.TracingInterceptor)


def test_retry_interceptor_recovers_from_unavailable(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(_rpc.time, "sleep", sleeps.append)

    attempts = {"n": 0}

    def call_next(request: object, ctx: RequestContext[object, object]) -> str:
        del ctx
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectError(Code.UNAVAILABLE, "connection refused")
        return f"ok:{request}"

    result = _rpc.RetryInterceptor().intercept_unary_sync(call_next, "req", _context())
    assert result == "ok:req"
    assert attempts["n"] == 3
    assert sleeps == [0.1, 0.2]


def test_retry_interceptor_gives_up_after_max_attempts(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(_rpc.time, "sleep", sleeps.append)

    def call_next(request: object, ctx: RequestContext[object, object]) -> str:
        del request, ctx
        raise ConnectError(Code.UNAVAILABLE, "connection refused")

    with pytest.raises(ConnectError) as exc_info:
        _rpc.RetryInterceptor().intercept_unary_sync(call_next, "req", _context())
    assert exc_info.value.code == Code.UNAVAILABLE
    assert sleeps == [0.1, 0.2, 0.4, 0.8]


def test_retry_interceptor_does_not_retry_other_codes(monkeypatch) -> None:
    monkeypatch.setattr(_rpc.time, "sleep", lambda _: None)
    attempts = {"n": 0}

    def call_next(request: object, ctx: RequestContext[object, object]) -> str:
        del request, ctx
        attempts["n"] += 1
        raise ConnectError(Code.RESOURCE_EXHAUSTED, "rate limited")

    with pytest.raises(ConnectError) as exc_info:
        _rpc.RetryInterceptor().intercept_unary_sync(call_next, "req", _context())
    assert exc_info.value.code == Code.RESOURCE_EXHAUSTED
    assert attempts["n"] == 1


def test_retry_backoff_caps_at_one_second(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(_rpc.time, "sleep", sleeps.append)
    monkeypatch.setattr(_rpc, "RPC_RETRY_MAX_ATTEMPTS", 6)

    def call_next(request: object, ctx: RequestContext[object, object]) -> str:
        del request, ctx
        raise ConnectError(Code.UNAVAILABLE, "blip")

    with pytest.raises(ConnectError):
        _rpc.RetryInterceptor().intercept_unary_sync(call_next, "req", _context())
    assert sleeps == [0.1, 0.2, 0.4, 0.8, 1.0]


@pytest.mark.parametrize("method", ["ExecuteSqlQuery", "PollSqlQuery"])
def test_sql_execution_and_polling_are_not_retried_by_transport(method) -> None:
    ctx = RequestContext(
        method=MethodInfo(
            name=method,
            service_name="chalk.protosql.v1.SqlService",
            input=object,
            output=object,
            idempotency_level=IdempotencyLevel.UNKNOWN,
        ),
        http_method="POST",
        request_headers=Headers(),
    )
    attempts = []

    def execute(request, context):
        attempts.append(request)
        raise ConnectError(Code.UNAVAILABLE, "response lost after submission")

    with pytest.raises(ConnectError):
        _rpc.RetryInterceptor().intercept_unary_sync(execute, "query", ctx)
    assert len(attempts) == 1
