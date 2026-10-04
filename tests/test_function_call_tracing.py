from __future__ import annotations

import asyncio
import contextvars
import time
from types import SimpleNamespace

import pyarrow as pa
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from chalkcompute import _tracing
from chalkcompute._function import FunctionCallHandle, RemoteFunction


@pytest.fixture
def tracing(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    monkeypatch.setattr(_tracing, "get_tracer", lambda: tracer)
    monkeypatch.setattr(_tracing, "_initialized", True)
    monkeypatch.setattr(_tracing, "_user_tracing_configured", True)
    monkeypatch.delenv("CHALK_REMOTE_FUNCTION_TRACE_POLICY", raising=False)
    monkeypatch.delenv("CHALK_FUNCTION_CALL_VIA_API", raising=False)
    yield tracer, exporter
    provider.shutdown()


@pytest.fixture
def function(monkeypatch):
    def add(a: int, b: int) -> int:
        return a + b

    fn = RemoteFunction(add)
    sink = pa.BufferOutputStream()
    batch = pa.record_batch([pa.array([332])], names=["result"])
    with pa.ipc.new_stream(sink, batch.schema) as writer:
        writer.write_batch(batch)
    response = sink.getvalue().to_pybytes()
    monkeypatch.setattr(fn, "_wait_for_background_deploy", lambda: None)
    monkeypatch.setattr(fn, "_call_direct_chunks_once", lambda *args: [response])
    return fn, response


@pytest.mark.parametrize("asynchronous", [False, True])
def test_direct_call_stages_share_the_request_trace(tracing, function, asynchronous):
    tracer, exporter = tracing
    fn, _ = function

    async def invoke():
        return await fn.remote(100, 232)

    with tracer.start_as_current_span("request") as request:
        result = asyncio.run(invoke()) if asynchronous else fn.remote(100, 232)

    assert result == 332
    spans = {s.name: s for s in exporter.get_finished_spans()}
    call = spans["chalkcompute.remote_function.call"]
    encode = spans["chalkcompute.remote_function.serialize_args"]
    rpc = spans["chalkcompute.remote_function.direct_rpc"]
    decode = spans["chalkcompute.remote_function.decode_result"]
    assert call.parent.span_id == request.get_span_context().span_id
    assert encode.parent.span_id == request.get_span_context().span_id
    assert rpc.parent.span_id == call.context.span_id
    assert decode.parent.span_id == call.context.span_id
    assert all(
        s.context.trace_id == request.get_span_context().trace_id
        for s in spans.values()
    )


def test_async_get_records_each_poll_and_decode_under_get(
    tracing, function, monkeypatch
):
    tracer, exporter = tracing
    fn, response = function
    polls = iter(
        [
            SimpleNamespace(
                cursor="next",
                poll_count=1,
                status_name="pending",
                chunks=[],
                is_expired=False,
                is_failed=False,
                is_completed=False,
            ),
            SimpleNamespace(
                cursor="done",
                poll_count=2,
                status_name="completed",
                chunks=[response],
                is_expired=False,
                is_failed=False,
                is_completed=True,
            ),
        ]
    )
    monkeypatch.setattr(fn, "poll", lambda *args, **kwargs: next(polls))
    handle = FunctionCallHandle(fn, "call-id")

    async def invoke():
        return await handle.get(timeout=1)

    with tracer.start_as_current_span("request") as request:
        assert asyncio.run(invoke()) == 332

    spans = exporter.get_finished_spans()
    get = next(s for s in spans if s.name == "chalkcompute.remote_function.get")
    children = [
        s
        for s in spans
        if s.name
        in {
            "chalkcompute.remote_function.poll",
            "chalkcompute.remote_function.decode_result",
        }
    ]
    assert get.parent.span_id == request.get_span_context().span_id
    assert [s.name for s in children].count("chalkcompute.remote_function.poll") == 2
    assert all(s.parent.span_id == get.context.span_id for s in children)


def test_remote_tracing_off_suppresses_call_stage_spans(tracing, function, monkeypatch):
    tracer, exporter = tracing
    fn, _ = function
    monkeypatch.setenv("CHALK_REMOTE_FUNCTION_TRACE_POLICY", "always_off")
    with tracer.start_as_current_span("request"):
        assert fn.remote(100, 232) == 332
    assert [s.name for s in exporter.get_finished_spans()] == ["request"]


def test_deadline_worker_preserves_and_isolates_context(function):
    fn, _ = function
    value = contextvars.ContextVar("request_value", default="unset")
    token = value.set("caller")

    def call():
        inherited = value.get()
        value.set("worker")
        return inherited

    try:
        assert (
            fn._call_with_deadline(call, time.monotonic() + 1, configured_timeout=1)
            == "caller"
        )
        assert value.get() == "caller"
    finally:
        value.reset(token)


@pytest.mark.parametrize("timeout", [None, 60])
def test_async_defer_forwards_trace_context_through_deadline_worker(
    tracing, function, monkeypatch, timeout
):
    tracer, _ = tracing
    fn, _ = function
    captured = []

    def enqueue(name, payload, *, metadata):
        captured.append(dict(metadata))
        return "call-id"

    monkeypatch.setattr(
        fn,
        "_ensure_remote_call_client",
        lambda **kwargs: SimpleNamespace(enqueue=enqueue),
    )
    fn._call_timeout = timeout

    async def invoke():
        return await asyncio.to_thread(fn.defer, 100, 232)

    with tracer.start_as_current_span("request") as request:
        assert asyncio.run(invoke()).call_id == "call-id"

    assert len(captured) == 1
    assert (
        captured[0]["traceparent"].split("-")[1]
        == f"{request.get_span_context().trace_id:032x}"
    )


@pytest.mark.parametrize("timeout", [None, 60])
def test_poll_forwards_its_current_trace_without_reusing_enqueue_context(
    tracing, function, monkeypatch, timeout
):
    tracer, _ = tracing
    fn, _ = function
    captured = []

    def poll_next(call_id, cursor, poll_count, *, metadata):
        captured.append(dict(metadata))
        return SimpleNamespace(error_message=None)

    monkeypatch.setattr(
        fn,
        "_ensure_remote_call_client",
        lambda **kwargs: SimpleNamespace(poll_next=poll_next),
    )
    fn._call_timeout = timeout
    for _ in range(2):
        with tracer.start_as_current_span("poll-request") as request:
            fn.poll("call-id")
            assert captured[-1]["traceparent"].split("-")[1] == (
                f"{request.get_span_context().trace_id:032x}"
            )
    assert captured[0]["traceparent"] != captured[1]["traceparent"]
