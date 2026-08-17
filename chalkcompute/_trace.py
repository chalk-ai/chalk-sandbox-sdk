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
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from chalkcompute._connect import ConnectClient
from chalkcompute._gen.chalk.arrow.v1 import arrow_pb2
from chalkcompute._tracing import _instrumented

_TRACE_SERVICE = "chalk.server.v1.TraceService"

SESSION_ID_ATTRIBUTE = "session.id"
"""Span attribute Chalk resolves an agent session from, first of six spellings.

Kept in sync with ``SESSION_ID_ATTRIBUTE_KEYS`` in chalk-private's
``engine/chalkengine/chalksql/providers/trace_query.py`` and the ClickHouse
``SessionId`` column expression.
"""

SESSION_ID_HEADER = "x-chalk-session-id"
"""Request header a caller sets to place a call's spans in a session.

Chalk sends it from ``catalog_call('fn', ..., metadata => {'x-chalk-session-id':
s})``; :func:`chalkcompute.get_call_context` exposes it, and the SDK stamps
:data:`SESSION_ID_ATTRIBUTE` from it automatically.
"""

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
    """

    id: str
    """The session id this handle queries."""

    _client: TraceClient | None = field(default=None, repr=False, compare=False)

    __chalk_arrow_type__ = arrow_pb2.ArrowType(large_utf8=arrow_pb2.EmptyMessage())

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

        Spans reach Chalk asynchronously — through a batching span processor,
        an OTLP collector, and finally ClickHouse — so a scorer running
        immediately after the task it scores can observe an *incomplete* set.
        A partial read produces a plausible wrong number rather than an error,
        so waiting is the default.

        Parameters
        ----------
        operation_name
            Only return spans with this operation name.
        attribute_filters
            Span attributes that must match.  A ``None`` value matches any span
            carrying the key.
        limit
            Stop after this many spans.  ``None`` returns all of them.
        wait
            Poll until the span count is stable across two consecutive polls.
            Pass ``False`` to read once, for a session that finished long ago.
        timeout
            Seconds to wait for the count to settle before raising
            :class:`TimeoutError`.  Never returns a knowingly partial list.
        poll_interval
            Seconds between polls.
        expect_min
            Minimum span count to wait for.  Defaults to 1, so an empty result
            is treated as "not ingested yet" rather than "no spans".  Pass 0 to
            accept an empty session.

        Returns
        -------
        list of Span
            The session's spans.

        Raises
        ------
        TimeoutError
            If ``timeout`` elapses before the count reaches ``expect_min`` and
            holds steady.
        """
        if timeout < 0:
            raise ValueError("timeout must be >= 0")
        if poll_interval < 0:
            raise ValueError("poll_interval must be >= 0")
        if expect_min is not None and expect_min < 0:
            raise ValueError("expect_min must be >= 0 or None")

        def fetch() -> list[Span]:
            return self._require_client().list_spans(
                self.id,
                operation_name=operation_name,
                attribute_filters=attribute_filters,
                limit=limit,
            )

        if not wait:
            return fetch()

        target = 1 if expect_min is None else expect_min
        deadline = time.monotonic() + timeout
        spans = fetch()
        previous = -1
        while True:
            if len(spans) >= target and len(spans) == previous:
                return spans
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if len(spans) < target:
                    raise TimeoutError(
                        f"session {self.id!r} had {len(spans)} span(s) after "
                        f"{timeout}s, expected at least {target}; spans may still "
                        f"be in flight. Pass wait=False to accept a partial read."
                    )
                raise TimeoutError(
                    f"session {self.id!r} was still receiving spans after "
                    f"{timeout}s ({previous} -> {len(spans)}); returning them "
                    f"would undercount. Raise timeout or pass wait=False."
                )
            previous = len(spans)
            time.sleep(min(poll_interval, remaining))
            spans = fetch()

    def summary(self) -> SessionSummary:
        """Return this session's aggregate: turn count, span count, wall clock."""
        return self._require_client().get_session(self.id)

    def traces(self, *, limit: int | None = None) -> list[TraceSummary]:
        """Return a summary per trace in this session — typically one per turn."""
        return list(self._require_client().list_traces(self.id, limit=limit))

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
        connect_client: ConnectClient | None = None,
    ) -> None:
        if chalk_client is not None and connect_client is not None:
            raise ValueError("pass only one of chalk_client or connect_client")
        self._connect = connect_client or ConnectClient(chalk_client=chalk_client)

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
            {"key": key} if value is None else {"key": key, "value": value}
            for key, value in (attribute_filters or {}).items()
        ]
        spans: list[Span] = []
        remaining = limit
        page_token: str | None = None
        while remaining is None or remaining > 0:
            request: dict[str, Any] = {
                "query": _session_query(session_id),
                "limit": (
                    _MAX_PAGE_SIZE
                    if remaining is None
                    else min(remaining, _MAX_PAGE_SIZE)
                ),
            }
            if operation_name is not None:
                request["operationName"] = operation_name
            if filters:
                request["attributeFilters"] = filters
            if page_token is not None:
                request["pageToken"] = page_token
            response = self._rpc("ListSpan", request)
            raw_spans = response.get("spans", [])
            if not isinstance(raw_spans, list):
                raise TraceError("ListSpan returned invalid spans")
            for raw in raw_spans:
                spans.append(_span_from_dict(raw))
                if remaining is not None:
                    remaining -= 1
                    if remaining <= 0:
                        return spans
            page_token = _optional_string(response.get("nextPageToken"))
            if page_token is None:
                return spans
        return spans

    @_instrumented("chalkcompute.trace.get_session")
    def get_session(self, session_id: str) -> SessionSummary:
        """Fetch one session's aggregate summary."""
        session_id = _require_id(session_id, "session_id")
        response = self._rpc("SearchSessions", {"sessionIds": [session_id], "limit": 1})
        raw_sessions = response.get("sessions", [])
        if not isinstance(raw_sessions, list):
            raise TraceError("SearchSessions returned invalid sessions")
        for raw in raw_sessions:
            if isinstance(raw, Mapping):
                return _session_from_dict(raw)
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
        while remaining is None or remaining > 0:
            request: dict[str, Any] = {
                "sessionIds": [session_id],
                "limit": (
                    _MAX_PAGE_SIZE
                    if remaining is None
                    else min(remaining, _MAX_PAGE_SIZE)
                ),
            }
            if page_token is not None:
                request["pageToken"] = page_token
            response = self._rpc("SearchTraceSummaries", request)
            raw_summaries = response.get("traceSummaries", [])
            if not isinstance(raw_summaries, list):
                raise TraceError("SearchTraceSummaries returned invalid summaries")
            for raw in raw_summaries:
                yield _trace_summary_from_dict(raw)
                if remaining is not None:
                    remaining -= 1
                    if remaining <= 0:
                        return
            page_token = _optional_string(response.get("nextPageToken"))
            if page_token is None:
                return

    def _rpc(self, method: str, request: dict[str, Any]) -> dict[str, Any]:
        response = self._connect.rpc(_TRACE_SERVICE, method, request)
        if response.status_code != 200:
            message = _response_error_message(response)
            detail = f": {message}" if message else ""
            if response.status_code in (401, 403):
                raise TraceError(
                    f"{method} was denied (HTTP {response.status_code}){detail}. "
                    f"Reading spans requires the monitoring-read permission; a "
                    f"deployed function's token must be granted it.",
                    status_code=response.status_code,
                )
            if response.status_code == 404:
                raise TraceNotFoundError(
                    f"{method} found nothing{detail}", status_code=response.status_code
                )
            raise TraceError(
                f"{method} failed with HTTP {response.status_code}{detail}",
                status_code=response.status_code,
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise TraceError(f"{method} returned an invalid response")
        return payload


def _session_query(session_id: str) -> str:
    return f"session:{session_id}"


def _span_from_dict(raw: object) -> Span:
    if not isinstance(raw, Mapping):
        raise TraceError("ListSpan returned an invalid span")
    status = raw.get("status")
    status = status if isinstance(status, Mapping) else {}
    return Span(
        span_id=_required_string(raw, "spanId"),
        trace_id=_required_string(raw, "traceId"),
        operation_name=_optional_string(raw.get("operationName")) or "",
        parent_span_id=_optional_string(raw.get("parentSpanId")),
        start_time=_parse_timestamp(raw.get("startTime")),
        end_time=_parse_timestamp(raw.get("endTime")),
        duration=_parse_duration(raw.get("duration")),
        status_code=_parse_status_code(status.get("code")),
        status_description=_optional_string(status.get("description")),
        kind=_parse_span_kind(raw.get("kind")),
        attributes=_string_mapping(raw.get("attributes")),
        resource_attributes=_string_mapping(raw.get("resourceAttributes")),
    )


def _session_from_dict(raw: Mapping[str, Any]) -> SessionSummary:
    return SessionSummary(
        session_id=_required_string(raw, "sessionId"),
        start_time=_parse_timestamp(raw.get("startTime")),
        end_time=_parse_timestamp(raw.get("endTime")),
        duration=_parse_duration(raw.get("duration")),
        trace_count=_parse_int(raw.get("traceCount")),
        span_count=_parse_int(raw.get("spanCount")),
        first_root_span_name=_optional_string(raw.get("firstRootSpanName")),
    )


def _trace_summary_from_dict(raw: object) -> TraceSummary:
    if not isinstance(raw, Mapping):
        raise TraceError("SearchTraceSummaries returned an invalid summary")
    names = raw.get("rootSpanNames")
    return TraceSummary(
        trace_id=_required_string(raw, "traceId"),
        start_time=_parse_timestamp(raw.get("startTime")),
        end_time=_parse_timestamp(raw.get("endTime")),
        duration=_parse_duration(raw.get("duration")),
        span_count=_parse_int(raw.get("spanCount")),
        root_span_names=tuple(
            value
            for value in (names if isinstance(names, Sequence) else ())
            if isinstance(value, str)
        ),
        session_id=_optional_string(raw.get("sessionId")),
    )


def _parse_span_kind(value: object) -> SpanKind:
    if isinstance(value, int):
        return {
            1: SpanKind.SERVER,
            2: SpanKind.CLIENT,
            3: SpanKind.PRODUCER,
            4: SpanKind.CONSUMER,
            5: SpanKind.INTERNAL,
        }.get(value, SpanKind.UNSPECIFIED)
    if isinstance(value, str):
        try:
            return SpanKind(value.removeprefix("CHALK_SPAN_KIND_").lower())
        except ValueError:
            pass
    return SpanKind.UNSPECIFIED


def _parse_status_code(value: object) -> SpanStatusCode:
    if isinstance(value, int):
        return {1: SpanStatusCode.OK, 2: SpanStatusCode.ERROR}.get(
            value, SpanStatusCode.UNSPECIFIED
        )
    if isinstance(value, str):
        try:
            return SpanStatusCode(value.removeprefix("CHALK_STATUS_CODE_").lower())
        except ValueError:
            pass
    return SpanStatusCode.UNSPECIFIED


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TraceError(f"invalid timestamp in trace response: {value!r}") from exc


def _parse_duration(value: object) -> timedelta | None:
    """Parse a protobuf JSON duration — a decimal number of seconds with an ``s``."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return timedelta(seconds=float(value))
    if not isinstance(value, str) or not value:
        return None
    try:
        return timedelta(seconds=float(value.removesuffix("s")))
    except ValueError as exc:
        raise TraceError(f"invalid duration in trace response: {value!r}") from exc


def _parse_int(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise TraceError(f"invalid count in trace response: {value!r}") from exc
    if isinstance(value, float):
        return int(value)
    raise TraceError(f"invalid count in trace response: {value!r}")


def _string_mapping(value: object) -> Mapping[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TraceError("trace response contained invalid attributes")
    return {str(key): str(item) for key, item in value.items()}


def _require_id(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{name} is required")
    return stripped


def _required_string(value: Mapping[str, Any], key: str) -> str:
    result = _optional_string(value.get(key))
    if result is None:
        raise TraceError(f"trace response is missing {key}")
    return result


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value


def _response_error_message(response: Any) -> str:
    try:
        payload = response.json()
    except Exception:
        return str(getattr(response, "text", "")).strip()
    if isinstance(payload, Mapping):
        message = payload.get("message")
        if isinstance(message, str) and message:
            return message
    return str(getattr(response, "text", "")).strip()
