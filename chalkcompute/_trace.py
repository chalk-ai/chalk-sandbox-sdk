"""Session-scoped telemetry, readable from inside a deployed function.

An evaluation scorer is handed a :class:`Trace` for the task invocation it is
scoring::

    @chalkcompute.function
    def total_cost(expected: str, output: str, trace: chalkcompute.Trace) -> float:
        return sum(
            span.attribute_float("llm.cost") or 0.0
            for span in trace.get_spans()
        )

``Trace`` is named for what a scorer means by "the trace" — the whole evaluated
unit of work, every turn and every nested LLM call.  Underneath it holds an
*agent session id* and every query it makes is session-scoped, because a
multi-turn agent emits one OpenTelemetry trace per turn: those traces are
siblings, not descendants, and only a session id groups them.  See
``docs/design/trace-propagation-in-scorers.md``.

The id travels as a ``large_utf8`` scalar, exactly as ``str`` would, so a
function may equally declare ``trace: str`` and call :meth:`Trace.from_id`.

An image built in strip mode does not install ``chalkcompute``, so the handler
shim publishes no call context there (it degrades rather than failing the call).
A function deployed that way is never placed in its caller's session, and
:meth:`Trace.get_spans` finds nothing — silently. Deploy from an environment with
a published ``chalkcompute`` for session-aware scoring.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, TypeVar

from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError
from protobuf import Oneof

from chalkcompute._connect import get_connect_client
from chalkcompute._gen.chalk.arrow.v1 import arrow_pb
from chalkcompute._gen.chalk.server.v1 import trace_connect, trace_pb
from chalkcompute._tracing import (
    SESSION_ID_ATTRIBUTE as SESSION_ID_ATTRIBUTE,
    SESSION_ID_HEADER as SESSION_ID_HEADER,
    _instrumented,
)

_T = TypeVar("_T")

_MAX_PAGE_SIZE = 1000


class TraceError(Exception):
    """Base exception for trace and span queries."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TraceNotFoundError(TraceError):
    """Raised when a session or span does not exist."""


class SpanKind(str, Enum):
    """OpenTelemetry span kind."""

    UNSPECIFIED = "unspecified"
    SERVER = "server"
    CLIENT = "client"
    PRODUCER = "producer"
    CONSUMER = "consumer"
    INTERNAL = "internal"


class SpanStatusCode(str, Enum):
    """OpenTelemetry span status."""

    UNSPECIFIED = "unspecified"
    OK = "ok"
    ERROR = "error"


@dataclass(frozen=True)
class Span:
    """One span of a session's telemetry."""

    span_id: str
    trace_id: str
    operation_name: str
    parent_span_id: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    duration: timedelta | None = None
    status_code: SpanStatusCode = SpanStatusCode.UNSPECIFIED
    status_description: str | None = None
    kind: SpanKind = SpanKind.UNSPECIFIED
    attributes: Mapping[str, str] = field(default_factory=dict)
    resource_attributes: Mapping[str, str] = field(default_factory=dict)

    @property
    def session_id(self) -> str | None:
        """Session this span was grouped into, if it carries one."""
        return self.attributes.get(
            SESSION_ID_ATTRIBUTE
        ) or self.resource_attributes.get(SESSION_ID_ATTRIBUTE)

    def attribute_float(self, key: str) -> float | None:
        """Return an attribute parsed as a float, or ``None`` if absent or unparseable.

        Span attributes arrive as strings — ``ChalkSpan.attributes`` is a
        ``map<string, string>`` — so summing ``span.attributes["llm.cost"]``
        directly concatenates text instead of adding numbers.  Use this instead.
        """
        raw = self.attributes.get(key)
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def attribute_int(self, key: str) -> int | None:
        """Return an attribute parsed as an int, or ``None`` if absent or unparseable."""
        value = self.attribute_float(key)
        if value is None:
            return None
        return int(value)


@dataclass(frozen=True)
class TraceSummary:
    """Aggregate over one OpenTelemetry trace — typically one agent turn."""

    trace_id: str
    start_time: datetime | None = None
    end_time: datetime | None = None
    duration: timedelta | None = None
    span_count: int = 0
    root_span_names: tuple[str, ...] = ()
    session_id: str | None = None


@dataclass(frozen=True)
class SessionSummary:
    """Aggregate over every trace sharing one session id."""

    session_id: str
    start_time: datetime | None = None
    end_time: datetime | None = None
    duration: timedelta | None = None
    trace_count: int = 0
    span_count: int = 0
    first_root_span_name: str | None = None


@dataclass(frozen=True)
class Trace:
    """A session's worth of telemetry: every trace and span under one call.

    Declaring a ``@chalkcompute.function`` parameter as ``Trace`` installs its
    Arrow contract, so the value arrives as the ``large_utf8`` session id Chalk
    bound to that argument.

    .. _trace-waiting:

    **Every read waits for ingest to settle.** Spans reach Chalk asynchronously
    — through a batching span processor, an OTLP collector, and finally
    ClickHouse — so a scorer running immediately after the task it scores can
    observe an incomplete session. That is worse than an error: a partial read
    produces a plausible wrong number. So :meth:`get_spans`, :meth:`summary`,
    and :meth:`traces` all poll until the number of spans they can see stops
    growing, and all take the same four arguments:

    ``wait``
        Poll until the span count holds steady across two consecutive reads.
        Pass ``False`` to read once, for a session that finished long ago.
    ``timeout``
        Seconds to wait for that. Exceeding it raises :class:`TimeoutError`
        rather than returning a knowingly incomplete result.
    ``poll_interval``
        Seconds between polls.
    ``expect_min``
        Minimum span count to wait for. Defaults to 1, so an empty result is
        treated as "not ingested yet" rather than "nothing to see". Pass 0 to
        accept an empty session.

    A steady count is a pragmatic signal, not a proof: it cannot tell "complete"
    from "quiet", so a long pause between agent turns can settle early. Pass
    ``expect_min`` when the caller knows better.
    """

    id: str
    """The session id this handle queries."""

    _client: TraceClient | None = field(default=None, repr=False, compare=False)

    __chalk_arrow_type__ = arrow_pb.ArrowType(
        arrow_type_enum=Oneof("large_utf8", arrow_pb.EmptyMessage())
    )

    def __post_init__(self) -> None:
        if not isinstance(self.id, str):
            raise TypeError("id must be a string")
        trace_id = self.id.strip()
        if not trace_id:
            raise ValueError("id is required")
        object.__setattr__(self, "id", trace_id)

    # -- Arrow contract ----------------------------------------------------

    @staticmethod
    def __chalk_pyarrow_type__() -> Any:
        import pyarrow as pa

        return pa.large_utf8()

    def __chalk_serialize__(self) -> Any:
        """Encode this handle as the session id string Chalk transports."""
        import pyarrow as pa

        return pa.scalar(self.id, type=pa.large_utf8())

    @classmethod
    def __chalk_deserialize__(cls, value: Any) -> Trace:
        """Decode a session id scalar into a handle."""
        return cls(value.as_py())

    # -- Construction ------------------------------------------------------

    @classmethod
    def from_id(cls, session_id: str, *, chalk_client: Any | None = None) -> Trace:
        """Attach to a session by id, for use outside a scorer."""
        return cls(session_id, _client=TraceClient(chalk_client=chalk_client))

    # -- Queries -----------------------------------------------------------

    def get_spans(
        self,
        *,
        operation_name: str | None = None,
        attribute_filters: Mapping[str, str | None] | None = None,
        limit: int | None = None,
        wait: bool = True,
        timeout: float = 60.0,
        poll_interval: float = 2.0,
        expect_min: int | None = None,
    ) -> list[Span]:
        """Return this session's spans, waiting for span ingest to settle.

        Parameters
        ----------
        operation_name
            Only return spans with this operation name.
        attribute_filters
            Span attributes that must match.  A ``None`` value matches any span
            carrying the key.
        limit
            Stop after this many spans.  ``None`` returns all of them.
        wait, timeout, poll_interval, expect_min
            See :ref:`the settling rules <trace-waiting>` on :class:`Trace`.

        Returns
        -------
        list of Span
            The session's spans.

        Raises
        ------
        TimeoutError
            If the span count does not reach ``expect_min`` and hold steady
            within ``timeout``.
        """

        def fetch() -> list[Span]:
            return self._require_client().list_spans(
                self.id,
                operation_name=operation_name,
                attribute_filters=attribute_filters,
                limit=limit,
            )

        return _settle(
            fetch,
            spans_seen=len,
            subject=f"session {self.id!r}",
            wait=wait,
            timeout=timeout,
            poll_interval=poll_interval,
            expect_min=expect_min,
        )

    def summary(
        self,
        *,
        wait: bool = True,
        timeout: float = 60.0,
        poll_interval: float = 2.0,
        expect_min: int | None = None,
    ) -> SessionSummary:
        """Return this session's aggregate: turn count, span count, wall clock.

        Settles on the same signal as :meth:`get_spans`, so ``trace_count`` and
        ``span_count`` do not undercount a session whose spans are still
        arriving.

        Parameters
        ----------
        wait, timeout, poll_interval, expect_min
            See :ref:`the settling rules <trace-waiting>` on :class:`Trace`.

        Returns
        -------
        SessionSummary
            The session's aggregate.

        Raises
        ------
        TraceNotFoundError
            If the session never appears — it received no spans at all.
        TimeoutError
            If it appears but its span count never holds steady.
        """

        ever_found = False

        def fetch() -> SessionSummary | None:
            nonlocal ever_found
            try:
                summary = self._require_client().get_session(self.id)
            except TraceNotFoundError:
                # An absent session is one that has received no spans yet, which
                # is the same "not ingested" state as an empty span list.
                return None
            ever_found = True
            return summary

        try:
            settled = _settle(
                fetch,
                spans_seen=lambda summary: 0 if summary is None else summary.span_count,
                subject=f"session {self.id!r}",
                wait=wait,
                timeout=timeout,
                poll_interval=poll_interval,
                expect_min=expect_min,
            )
        except TimeoutError:
            # "It never showed up" is the more actionable diagnosis, and keeps the
            # exception type the same as the wait=False path.
            if not ever_found:
                raise TraceNotFoundError(
                    f"Session not found: {self.id} (waited {timeout}s)"
                ) from None
            raise
        if settled is None:
            raise TraceNotFoundError(f"Session not found: {self.id}")
        return settled

    def traces(
        self,
        *,
        limit: int | None = None,
        wait: bool = True,
        timeout: float = 60.0,
        poll_interval: float = 2.0,
        expect_min: int | None = None,
    ) -> list[TraceSummary]:
        """Return a summary per trace in this session — typically one per turn.

        Settles on the same signal as :meth:`get_spans`: a turn that is still
        recording grows this session's span count without adding a trace, so
        waiting on the traces alone would return stale per-trace counts.

        Parameters
        ----------
        limit
            Stop after this many traces.  ``None`` returns all of them.
        wait, timeout, poll_interval, expect_min
            See :ref:`the settling rules <trace-waiting>` on :class:`Trace`.

        Returns
        -------
        list of TraceSummary
            One summary per trace, newest first.

        Raises
        ------
        TimeoutError
            If the session's span count does not reach ``expect_min`` and hold
            steady within ``timeout``.
        """

        def fetch() -> list[TraceSummary]:
            return list(self._require_client().list_traces(self.id, limit=limit))

        return _settle(
            fetch,
            spans_seen=lambda summaries: sum(s.span_count for s in summaries),
            subject=f"session {self.id!r}",
            wait=wait,
            timeout=timeout,
            poll_interval=poll_interval,
            expect_min=expect_min,
        )

    def _require_client(self) -> TraceClient:
        if self._client is None:
            object.__setattr__(self, "_client", TraceClient())
        client = self._client
        assert client is not None
        return client


class TraceClient:
    """Client for span and session queries against ``chalk.server.v1.TraceService``."""

    def __init__(
        self,
        *,
        chalk_client: Any | None = None,
    ) -> None:
        self._connect = get_connect_client(chalk_client)

    @_instrumented("chalkcompute.trace.list_spans")
    def list_spans(
        self,
        session_id: str,
        *,
        operation_name: str | None = None,
        attribute_filters: Mapping[str, str | None] | None = None,
        limit: int | None = None,
    ) -> list[Span]:
        """List a session's spans, paging transparently.

        ``ListSpan`` has no session filter, so the session is expressed through
        the faceted ``query`` string, whose ``session:`` key Chalk resolves
        against the indexed ``SessionId`` column.
        """
        session_id = _require_id(session_id, "session_id")
        if limit is not None and limit < 0:
            raise ValueError("limit must be >= 0 or None")

        filters = [
            trace_pb.AttributeFilter(key=key, value=value)
            for key, value in (attribute_filters or {}).items()
        ]
        spans: list[Span] = []
        remaining = limit
        page_token: str | None = None
        stub = self._connect.stub(trace_connect.TraceServiceClientSync)
        while remaining is None or remaining > 0:
            response = self._call(
                stub.list_span,
                trace_pb.ListSpanRequest(
                    query=_session_query(session_id),
                    limit=(
                        _MAX_PAGE_SIZE
                        if remaining is None
                        else min(remaining, _MAX_PAGE_SIZE)
                    ),
                    operation_name=operation_name,
                    attribute_filters=filters,
                    page_token=page_token,
                ),
            )
            for raw in response.spans:
                spans.append(_span_from_proto(raw))
                if remaining is not None:
                    remaining -= 1
                    if remaining <= 0:
                        return spans
            page_token = response.next_page_token or None
            if page_token is None:
                return spans
        return spans

    @_instrumented("chalkcompute.trace.get_session")
    def get_session(self, session_id: str) -> SessionSummary:
        """Fetch one session's aggregate summary."""
        session_id = _require_id(session_id, "session_id")
        stub = self._connect.stub(trace_connect.TraceServiceClientSync)
        response = self._call(
            stub.search_sessions,
            trace_pb.SearchSessionsRequest(session_ids=[session_id], limit=1),
        )
        for raw in response.sessions:
            return _session_from_proto(raw)
        raise TraceNotFoundError(f"Session not found: {session_id}")

    @_instrumented("chalkcompute.trace.list_traces")
    def list_traces(
        self, session_id: str, *, limit: int | None = None
    ) -> Iterator[TraceSummary]:
        """Iterate a session's traces, newest first, paging transparently."""
        session_id = _require_id(session_id, "session_id")
        if limit is not None and limit < 0:
            raise ValueError("limit must be >= 0 or None")
        remaining = limit
        page_token: str | None = None
        stub = self._connect.stub(trace_connect.TraceServiceClientSync)
        while remaining is None or remaining > 0:
            response = self._call(
                stub.search_trace_summaries,
                trace_pb.SearchTraceSummariesRequest(
                    session_ids=[session_id],
                    limit=(
                        _MAX_PAGE_SIZE
                        if remaining is None
                        else min(remaining, _MAX_PAGE_SIZE)
                    ),
                    page_token=page_token,
                ),
            )
            for raw in response.trace_summaries:
                yield _trace_summary_from_proto(raw)
                if remaining is not None:
                    remaining -= 1
                    if remaining <= 0:
                        return
            page_token = response.next_page_token or None
            if page_token is None:
                return

    def _call(self, method: Any, request: Any) -> Any:
        method_name = getattr(method, "__name__", "trace RPC")
        try:
            return self._connect.call(method, request)
        except ConnectRpcError as exc:
            status_code = _http_status_for_code(exc.code)
            if exc.code in (Code.UNAUTHENTICATED, Code.PERMISSION_DENIED):
                raise TraceError(
                    f"{method_name} was denied: {exc}. "
                    f"Reading spans requires the monitoring-read permission; a "
                    f"deployed function's token must be granted it.",
                    status_code=status_code,
                ) from exc
            if exc.code == Code.NOT_FOUND:
                raise TraceNotFoundError(
                    f"{method_name} found nothing: {exc}", status_code=status_code
                ) from exc
            raise TraceError(
                f"{method_name} failed: {exc}",
                status_code=status_code,
            ) from exc


def _settle(
    fetch: Callable[[], _T],
    *,
    spans_seen: Callable[[_T], int],
    subject: str,
    wait: bool,
    timeout: float,
    poll_interval: float,
    expect_min: int | None,
) -> _T:
    """Poll ``fetch`` until the number of spans it can see stops growing.

    Every read of a session settles on the same signal, because every read is
    downstream of the same asynchronous ingest: spans, a session aggregate, and
    per-trace aggregates all grow as spans land. ``spans_seen`` extracts that
    one number from whatever ``fetch`` returns.

    Returns the settled payload. Raises :class:`TimeoutError` rather than
    returning a knowingly incomplete one — an undercount reads as a real result.
    """
    if timeout < 0:
        raise ValueError("timeout must be >= 0")
    if poll_interval < 0:
        raise ValueError("poll_interval must be >= 0")
    if expect_min is not None and expect_min < 0:
        raise ValueError("expect_min must be >= 0 or None")

    if not wait:
        return fetch()

    target = 1 if expect_min is None else expect_min
    deadline = time.monotonic() + timeout
    result = fetch()
    count = spans_seen(result)
    previous = -1
    while True:
        if count >= target and count == previous:
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if count < target:
                raise TimeoutError(
                    f"{subject} had {count} span(s) after {timeout}s, expected at "
                    f"least {target}; spans may still be in flight. Pass "
                    f"wait=False to accept a partial read."
                )
            growth = (
                "the count was never confirmed steady"
                if previous < 0
                else f"the count went {previous} -> {count}"
            )
            raise TimeoutError(
                f"{subject} was still receiving spans after {timeout}s "
                f"({growth}); returning now would undercount. Raise timeout or "
                f"pass wait=False."
            )
        previous = count
        time.sleep(min(poll_interval, remaining))
        result = fetch()
        count = spans_seen(result)


def _session_query(session_id: str) -> str:
    return f"session:{session_id}"


def _span_from_proto(raw: trace_pb.ChalkSpan) -> Span:
    status = raw.status
    return Span(
        span_id=raw.span_id,
        trace_id=raw.trace_id,
        operation_name=raw.operation_name,
        parent_span_id=raw.parent_span_id or None,
        start_time=None if raw.start_time is None else raw.start_time.to_datetime(),
        end_time=None if raw.end_time is None else raw.end_time.to_datetime(),
        duration=None if raw.duration is None else raw.duration.to_timedelta(),
        status_code=(
            SpanStatusCode.UNSPECIFIED
            if status is None
            else _parse_status_code(status.code)
        ),
        status_description=None if status is None else status.description or None,
        kind=_parse_span_kind(raw.kind),
        attributes=dict(raw.attributes),
        resource_attributes=dict(raw.resource_attributes),
    )


def _session_from_proto(raw: trace_pb.ChalkSessionSummary) -> SessionSummary:
    return SessionSummary(
        session_id=raw.session_id,
        start_time=None if raw.start_time is None else raw.start_time.to_datetime(),
        end_time=None if raw.end_time is None else raw.end_time.to_datetime(),
        duration=None if raw.duration is None else raw.duration.to_timedelta(),
        trace_count=raw.trace_count,
        span_count=raw.span_count,
        first_root_span_name=raw.first_root_span_name or None,
    )


def _trace_summary_from_proto(raw: trace_pb.ChalkTraceSummary) -> TraceSummary:
    return TraceSummary(
        trace_id=raw.trace_id,
        start_time=None if raw.start_time is None else raw.start_time.to_datetime(),
        end_time=None if raw.end_time is None else raw.end_time.to_datetime(),
        duration=None if raw.duration is None else raw.duration.to_timedelta(),
        span_count=raw.span_count,
        root_span_names=tuple(raw.root_span_names),
        session_id=raw.session_id or None,
    )


def _parse_span_kind(value: trace_pb.ChalkSpanKind) -> SpanKind:
    try:
        return SpanKind(value.name.lower())
    except ValueError:
        return SpanKind.UNSPECIFIED


def _parse_status_code(value: trace_pb.ChalkStatusCode) -> SpanStatusCode:
    try:
        return SpanStatusCode(value.name.lower())
    except ValueError:
        return SpanStatusCode.UNSPECIFIED


def _require_id(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{name} is required")
    return stripped


def _http_status_for_code(code: Code) -> int | None:
    return {
        Code.INVALID_ARGUMENT: 400,
        Code.UNAUTHENTICATED: 401,
        Code.PERMISSION_DENIED: 403,
        Code.NOT_FOUND: 404,
        Code.RESOURCE_EXHAUSTED: 429,
        Code.INTERNAL: 500,
        Code.UNAVAILABLE: 503,
    }.get(code)
