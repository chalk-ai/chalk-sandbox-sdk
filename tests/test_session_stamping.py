"""A caller's ``x-chalk-session-id`` header groups the call's spans into a session.

Chalk resolves an agent session from the ``session.id`` *span* attribute, so the id
has to reach every span before export — including spans this SDK never creates, such
as those from LLM instrumentation. The header enters the row's OpenTelemetry context
as baggage, and a provider-level span processor copies it onto each span at start.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pyarrow as pa
import pytest
from opentelemetry import baggage, context as otel_context, trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from chalkcompute import _tracing
from chalkcompute._call_context import (
    get_call_context,
    reset_call_scope,
    set_call_scope,
)
from chalkcompute._function import _generate_handler_shim
from chalkcompute._trace import SESSION_ID_ATTRIBUTE, SESSION_ID_HEADER

_USER_MODULE = "user_module"


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Install a real recording provider and let the SDK stamp spans on it."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    monkeypatch.setattr(_tracing, "_session_processor_installed", False)
    monkeypatch.setattr(_tracing, "_initialized", True)
    monkeypatch.setattr(_tracing, "_provider", provider)
    monkeypatch.setattr(_tracing, "_tracer", None)
    monkeypatch.setattr(trace_api, "get_tracer_provider", lambda: provider)
    assert _tracing._install_session_processor(provider)
    return exporter


def _span_session_ids(exporter: InMemorySpanExporter) -> list[str | None]:
    return [
        span.attributes.get(SESSION_ID_ATTRIBUTE) if span.attributes else None
        for span in exporter.get_finished_spans()
    ]


def _emit(name: str = "llm.completion") -> None:
    """Emit a span through an independent tracer, as LLM instrumentation would."""
    trace_api.get_tracer("third-party-instrumentation").start_span(name).end()


# --- The processor --------------------------------------------------------


def test_a_span_started_in_a_session_carries_the_session_id(
    exporter: InMemorySpanExporter,
) -> None:
    token = _tracing.enter_session("session-1")
    try:
        _emit()
    finally:
        _tracing.exit_session(token)

    assert _span_session_ids(exporter) == ["session-1"]


def test_spans_from_a_tracer_the_sdk_does_not_own_are_stamped(
    exporter: InMemorySpanExporter,
) -> None:
    """The point of a provider-level processor: it sees every tracer's spans."""
    token = _tracing.enter_session("session-1")
    try:
        trace_api.get_tracer("openinference.anthropic").start_span("chat").end()
        trace_api.get_tracer("some.other.library").start_span("tool").end()
    finally:
        _tracing.exit_session(token)

    assert _span_session_ids(exporter) == ["session-1", "session-1"]


def test_nested_spans_inherit_the_session(exporter: InMemorySpanExporter) -> None:
    token = _tracing.enter_session("session-1")
    try:
        tracer = trace_api.get_tracer("agent")
        with tracer.start_as_current_span("turn"):
            with tracer.start_as_current_span("llm.completion"):
                pass
    finally:
        _tracing.exit_session(token)

    assert _span_session_ids(exporter) == ["session-1", "session-1"]


def test_spans_outside_a_session_are_left_alone(
    exporter: InMemorySpanExporter,
) -> None:
    _emit()
    assert _span_session_ids(exporter) == [None]


def test_exiting_a_session_stops_the_stamping(exporter: InMemorySpanExporter) -> None:
    token = _tracing.enter_session("session-1")
    _emit("inside")
    _tracing.exit_session(token)
    _emit("outside")

    assert _span_session_ids(exporter) == ["session-1", None]


def test_sessions_do_not_leak_between_rows(exporter: InMemorySpanExporter) -> None:
    """One pod serves rows from many sessions; a resource attribute could not."""
    for session_id in ("session-1", "session-2"):
        token = _tracing.enter_session(session_id)
        try:
            _emit()
        finally:
            _tracing.exit_session(token)

    assert _span_session_ids(exporter) == ["session-1", "session-2"]


def test_installing_the_processor_twice_stamps_once(
    exporter: InMemorySpanExporter,
) -> None:
    provider = trace_api.get_tracer_provider()
    assert _tracing._install_session_processor(provider) is True  # already installed

    token = _tracing.enter_session("session-1")
    try:
        _emit()
    finally:
        _tracing.exit_session(token)

    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert span.attributes[SESSION_ID_ATTRIBUTE] == "session-1"


def test_a_no_op_provider_is_declined() -> None:
    """The API's no-op provider creates no spans, so there is nothing to stamp."""
    assert _tracing._install_session_processor(object()) is False


def test_enter_session_is_inert_when_tracing_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHALK_REMOTE_FUNCTION_TRACE_POLICY", "always_off")
    assert _tracing.enter_session("session-1") is None


def test_enter_session_ignores_an_empty_id() -> None:
    assert _tracing.enter_session("") is None


# --- The call scope ------------------------------------------------------


def test_the_call_scope_publishes_the_session_from_the_header(
    exporter: InMemorySpanExporter,
) -> None:
    token = set_call_scope({SESSION_ID_HEADER: "session-1"}, None)
    try:
        assert _tracing.current_session_id() == "session-1"
        _emit()
    finally:
        reset_call_scope(token)

    assert _span_session_ids(exporter) == ["session-1"]
    assert _tracing.current_session_id() is None


def test_the_header_is_matched_case_insensitively(
    exporter: InMemorySpanExporter,
) -> None:
    """gRPC metadata keys are case-insensitive."""
    token = set_call_scope({"X-Chalk-Session-Id": "session-1"}, None)
    try:
        assert _tracing.current_session_id() == "session-1"
    finally:
        reset_call_scope(token)


def test_a_blank_session_header_publishes_nothing() -> None:
    token = set_call_scope({SESSION_ID_HEADER: "   "}, None)
    try:
        assert token.session is None
        assert _tracing.current_session_id() is None
    finally:
        reset_call_scope(token)


def test_a_scope_without_the_header_leaves_the_context_alone() -> None:
    token = set_call_scope({"traceparent": "00-abc-def-01"}, None)
    try:
        assert token.session is None
        assert _tracing.current_session_id() is None
    finally:
        reset_call_scope(token)


def test_the_scope_still_publishes_the_headers_it_always_did() -> None:
    token = set_call_scope({SESSION_ID_HEADER: "session-1"}, b"blob")
    try:
        assert get_call_context()[SESSION_ID_HEADER] == "session-1"
    finally:
        reset_call_scope(token)


def test_resetting_restores_an_outer_session() -> None:
    outer = otel_context.attach(baggage.set_baggage(SESSION_ID_ATTRIBUTE, "outer"))
    try:
        token = set_call_scope({SESSION_ID_HEADER: "inner"}, None)
        assert _tracing.current_session_id() == "inner"
        reset_call_scope(token)
        assert _tracing.current_session_id() == "outer"
    finally:
        otel_context.detach(outer)


# --- End to end through the generated shim -------------------------------


def test_a_function_row_emits_spans_into_the_callers_session(
    exporter: InMemorySpanExporter,
) -> None:
    """The whole point: a task's spans land in the session the runner minted."""
    seen: list[str | None] = []

    def echo(x: int) -> int:
        seen.append(_tracing.current_session_id())
        _emit()
        return x

    module = types.ModuleType(_USER_MODULE)
    module.echo = echo  # type: ignore[attr-defined]
    previous = sys.modules.get(_USER_MODULE)
    sys.modules[_USER_MODULE] = module
    try:
        namespace: dict[str, Any] = {}
        exec(compile(_generate_handler_shim("echo"), "<shim>", "exec"), namespace)
        assert namespace["handler"](
            {"x": pa.array([1, 2])}, {SESSION_ID_HEADER: "session-1"}
        ) == [1, 2]
    finally:
        if previous is None:
            sys.modules.pop(_USER_MODULE, None)
        else:
            sys.modules[_USER_MODULE] = previous

    assert seen == ["session-1", "session-1"]
    assert _span_session_ids(exporter) == ["session-1", "session-1"]
