"""A caller's ``x-chalk-session-id`` header groups the call's spans into a session.

Chalk resolves an agent session from the ``session.id`` *span* attribute, so the id
has to reach every span before export — including spans this SDK never creates, such
as those from LLM instrumentation. The header enters the row's OpenTelemetry context
as baggage, and a provider-level span processor copies it onto each span at start.
"""

from __future__ import annotations

import sys
import types
import weakref
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

    monkeypatch.setattr(_tracing, "_session_processor_providers", weakref.WeakSet())
    monkeypatch.setattr(_tracing, "_initialized", True)
    monkeypatch.setattr(_tracing, "_provider", provider)
    monkeypatch.setattr(_tracing, "_tracer", None)
    monkeypatch.setattr(trace_api, "get_tracer_provider", lambda: provider)
    assert _tracing._install_session_processor(provider)
    return exporter


def _emitted(exporter: InMemorySpanExporter) -> list[Any]:
    """The spans the caller emitted, without the roots ``enter_session`` opens itself."""
    return [
        span
        for span in exporter.get_finished_spans()
        if span.name != _tracing.SESSION_ROOT_SPAN_NAME
    ]


def _session_roots(exporter: InMemorySpanExporter) -> list[Any]:
    return [
        span
        for span in exporter.get_finished_spans()
        if span.name == _tracing.SESSION_ROOT_SPAN_NAME
    ]


def _span_session_ids(exporter: InMemorySpanExporter) -> list[str | None]:
    return [
        span.attributes.get(SESSION_ID_ATTRIBUTE) if span.attributes else None
        for span in _emitted(exporter)
    ]


def _trace_ids(spans: list[Any]) -> list[int]:
    return [span.get_span_context().trace_id for span in spans]


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

    (span,) = _emitted(exporter)
    assert span.attributes is not None
    assert span.attributes[SESSION_ID_ATTRIBUTE] == "session-1"


def test_a_no_op_provider_is_declined() -> None:
    """The API's no-op provider creates no spans, so there is nothing to stamp."""
    assert _tracing._install_session_processor(object()) is False


def test_each_provider_is_stamped_even_after_another_one_was(
    exporter: InMemorySpanExporter,
) -> None:
    """Losing the ``set_tracer_provider`` race must not disarm the winner.

    In a function pod the remote-call runtime installs its provider first, so the one
    chalkcompute builds is refused and orphaned. A process-wide "already installed" flag
    let that orphan's install suppress the live provider's, and nothing exported was ever
    stamped.
    """
    orphan = TracerProvider()
    assert _tracing._install_session_processor(orphan) is True

    live = trace_api.get_tracer_provider()
    assert _tracing._install_session_processor(live) is True

    token = _tracing.enter_session("session-1")
    try:
        _emit()
    finally:
        _tracing.exit_session(token)

    assert _span_session_ids(exporter) == ["session-1"]


def test_a_provider_someone_else_installed_is_adopted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Building a second provider would leave it holding an exporter no span reaches."""
    installed = TracerProvider()
    monkeypatch.setattr(_tracing, "_session_processor_providers", weakref.WeakSet())
    monkeypatch.setattr(_tracing, "_provider", None)
    monkeypatch.setattr(_tracing, "_tracer", None)
    monkeypatch.setattr(trace_api, "get_tracer_provider", lambda: installed)
    calls: list[Any] = []
    monkeypatch.setattr(trace_api, "set_tracer_provider", calls.append)

    _tracing._install_tracer_provider(sample_rate=1.0)

    assert _tracing._provider is installed
    assert calls == []


# --- One trace, one session ----------------------------------------------


def test_two_sessions_under_one_call_do_not_share_a_trace(
    exporter: InMemorySpanExporter,
) -> None:
    """The bug this isolation exists for.

    A remote call serves a whole batch under one invocation span. Chalk resolves a trace's
    session as a ``max()`` over its spans, so rows sharing that trace would collapse into
    one session.
    """
    with trace_api.get_tracer("runtime").start_as_current_span("invoke") as invoke:
        for session_id in ("session-1", "session-2"):
            token = _tracing.enter_session(session_id)
            try:
                _emit()
            finally:
                _tracing.exit_session(token)
        invoke_trace = invoke.get_span_context().trace_id

    rows = [span for span in _emitted(exporter) if span.name == "llm.completion"]
    assert [span.attributes[SESSION_ID_ATTRIBUTE] for span in rows] == [
        "session-1",
        "session-2",
    ]
    assert len(set(_trace_ids(rows))) == 2
    assert invoke_trace not in _trace_ids(rows)


def test_a_session_trace_links_back_to_the_call_that_opened_it(
    exporter: InMemorySpanExporter,
) -> None:
    """Severing the trace must not sever the trail between the call and its rows."""
    with trace_api.get_tracer("runtime").start_as_current_span("invoke") as invoke:
        enclosing = invoke.get_span_context()
        token = _tracing.enter_session("session-1")
        _tracing.exit_session(token)

    (root,) = _session_roots(exporter)
    (link,) = root.links
    assert link.context.trace_id == enclosing.trace_id
    assert link.context.span_id == enclosing.span_id


def test_a_session_with_no_spans_of_its_own_is_still_visible(
    exporter: InMemorySpanExporter,
) -> None:
    token = _tracing.enter_session("session-1")
    _tracing.exit_session(token)

    (root,) = _session_roots(exporter)
    assert root.attributes is not None
    assert root.attributes[SESSION_ID_ATTRIBUTE] == "session-1"


def test_sibling_spans_of_one_row_stay_in_one_trace(
    exporter: InMemorySpanExporter,
) -> None:
    """Owning a root, rather than only cutting the context, is what buys this."""
    token = _tracing.enter_session("session-1")
    try:
        _emit("first")
        _emit("second")
    finally:
        _tracing.exit_session(token)

    assert len(set(_trace_ids(_emitted(exporter)))) == 1


def test_re_entering_the_same_session_stays_in_its_trace(
    exporter: InMemorySpanExporter,
) -> None:
    """A nested compute function inherits the session; it must not split the conversation."""
    outer = _tracing.enter_session("session-1")
    try:
        _emit("outer")
        inner = _tracing.enter_session("session-1")
        try:
            _emit("inner")
        finally:
            _tracing.exit_session(inner)
    finally:
        _tracing.exit_session(outer)

    assert len(_session_roots(exporter)) == 1
    assert len(set(_trace_ids(_emitted(exporter)))) == 1


def test_a_session_entered_outside_any_span_has_nothing_to_link(
    exporter: InMemorySpanExporter,
) -> None:
    token = _tracing.enter_session("session-1")
    _tracing.exit_session(token)

    (root,) = _session_roots(exporter)
    assert root.links == ()
    assert root.parent is None


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
