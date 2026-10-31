"""Session-scoped telemetry, readable from inside a deployed function.

An evaluation scorer is handed a :class:`Trace` for the task invocation it is
scoring::

    @chalkcompute.function
    def total_cost(expected: str, output: str, trace: chalkcompute.Trace) -> float:
        return sum(
            span.cost or 0.0
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

:meth:`Trace.transcript` renders the session's LLM turns as text. It is what
:func:`chalkcompute.scorers.llm_judge` shows the model when a judge names
``trace`` among its inputs, and what a notebook reads to follow an agent run.

An image built in strip mode does not install ``chalkcompute``, so the handler
shim publishes no call context there (it degrades rather than failing the call).
A function deployed that way is never placed in its caller's session, and
:meth:`Trace.get_spans` finds nothing — silently. Deploy from an environment with
a published ``chalkcompute`` for session-aware scoring.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, TypeVar

from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError
from protobuf import Oneof

from chalkcompute._connect import ConnectClient
from chalkcompute._gen.chalk.arrow.v1 import arrow_pb
from chalkcompute._gen.chalk.server.v1 import trace_connect, trace_pb
from chalkcompute._tracing import (
    SESSION_ID_ATTRIBUTE as SESSION_ID_ATTRIBUTE,
    SESSION_ID_HEADER as SESSION_ID_HEADER,
    _instrumented,
)

_T = TypeVar("_T")

_MAX_PAGE_SIZE = 1000

# Chalk router metadata for bodies that are externalized, omitted or truncated.
_CONTENT_METADATA = ("content_uri", "content_bytes", "upload_omitted", "truncation")
# OpenInference names shared by chalk-router and instrumentations.
_MODEL_NAME_ATTRIBUTE = "llm.model_name"
_TOKEN_TOTAL_ATTRIBUTE = "llm.token_count.total"
_NO_TURNS_NOTE = "(no LLM turns were recorded in this session)"


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
    """One span of a session's telemetry.

    Token, cost, model and provider properties normalize common telemetry
    attributes without making additional requests. Numeric properties return
    ``None`` when unavailable or invalid; a recorded zero remains zero.
    Metrics describe this span only, never an aggregate over its children.
    """

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

    @property
    def model(self) -> str | None:
        """Model used, preferring Chalk's normalized value and the response model."""
        return self._first_attribute(
            "chalk.ai.model",
            "gen_ai.response.model",
            _MODEL_NAME_ATTRIBUTE,
            "gen_ai.request.model",
        )

    @property
    def provider(self) -> str | None:
        """LLM provider, from OpenInference or GenAI attributes."""
        return self._first_attribute(
            "llm.provider", "gen_ai.provider.name", "gen_ai.system"
        )

    @property
    def input_tokens(self) -> int | None:
        """Prompt tokens, including any cache tokens already included by the emitter."""
        return self._token_count(
            "chalk.ai.input_tokens",
            "llm.token_count.prompt",
            "gen_ai.usage.input_tokens",
        )

    @property
    def output_tokens(self) -> int | None:
        """Completion tokens, including any reasoning tokens reported in that total."""
        return self._token_count(
            "chalk.ai.output_tokens",
            "llm.token_count.completion",
            "gen_ai.usage.output_tokens",
        )

    @property
    def total_tokens(self) -> int | None:
        """Recorded total, or input plus output when both counts are available.

        Cache and reasoning counts are breakdowns and are not added again.
        A partial count does not imply the missing count is zero.
        """
        total = self._token_count(
            "chalk.ai.total_tokens", _TOKEN_TOTAL_ATTRIBUTE, "gen_ai.usage.total_tokens"
        )
        if total is not None:
            return total
        input_tokens, output_tokens = self.input_tokens, self.output_tokens
        if input_tokens is None or output_tokens is None:
            return None
        return input_tokens + output_tokens

    @property
    def cache_read_tokens(self) -> int | None:
        """Prompt tokens read from the provider's cache."""
        return self._token_count(
            "chalk.ai.cache_read_tokens",
            "llm.token_count.prompt_details.cache_read",
            "gen_ai.usage.cache_read.input_tokens",
        )

    @property
    def cache_write_tokens(self) -> int | None:
        """Prompt tokens written to the provider's cache."""
        return self._token_count(
            "chalk.ai.cache_write_tokens",
            "llm.token_count.prompt_details.cache_write",
            "gen_ai.usage.cache_creation.input_tokens",
        )

    @property
    def reasoning_tokens(self) -> int | None:
        """Reasoning tokens within the completion, when reported."""
        return self._token_count(
            "llm.token_count.completion_details.reasoning",
            "gen_ai.usage.reasoning.output_tokens",
        )

    @property
    def cost(self) -> float | None:
        """Recorded total cost in USD; no model pricing is inferred.

        Tries Chalk's normalized cost, OpenInference total/legacy cost, then
        GenAI cost, skipping malformed, negative and non-finite values.
        """
        for key in (
            "chalk.ai.cost_usd",
            "llm.cost.total",
            "llm.cost",
            "gen_ai.usage.cost",
        ):
            value = self.attribute_float(key)
            if value is not None and value >= 0:
                return value
        return None

    @property
    def metrics(self) -> dict[str, int | float]:
        """Available usage and timing metrics, using Braintrust metric names.

        Includes ``tokens``, ``prompt_tokens``, ``completion_tokens``,
        ``prompt_cached_tokens``, ``prompt_cache_creation_tokens``,
        ``completion_reasoning_tokens`` and ``estimated_cost`` when available.
        ``start`` and ``end`` are Unix seconds; ``duration`` is seconds.
        Missing metrics are omitted. The returned dictionary is a fresh copy.
        """
        values: dict[str, int | float | None] = {
            "tokens": self.total_tokens,
            "prompt_tokens": self.input_tokens,
            "completion_tokens": self.output_tokens,
            "prompt_cached_tokens": self.cache_read_tokens,
            "prompt_cache_creation_tokens": self.cache_write_tokens,
            "completion_reasoning_tokens": self.reasoning_tokens,
            "estimated_cost": self.cost,
            "start": self.start_time.timestamp()
            if self.start_time is not None
            else None,
            "end": self.end_time.timestamp() if self.end_time is not None else None,
            "duration": self.duration.total_seconds()
            if self.duration is not None
            else None,
        }
        return {key: value for key, value in values.items() if value is not None}

    def _first_attribute(self, *keys: str) -> str | None:
        return next(
            (self.attributes[key] for key in keys if self.attributes.get(key)), None
        )

    def _token_count(self, *keys: str) -> int | None:
        # Unlike the generic integer accessor, token counts must not truncate
        # fractional values or accept negative counts.
        for key in keys:
            raw = self.attributes.get(key)
            if raw is None:
                continue
            try:
                value = Decimal(raw)
            except InvalidOperation:
                continue
            if value.is_finite() and value >= 0 and value == value.to_integral_value():
                return int(value)
        return None

    def attribute_float(self, key: str) -> float | None:
        """Return a finite float, or ``None`` if absent, unparseable or non-finite.

        Span attributes arrive as strings — ``ChalkSpan.attributes`` is a
        ``map<string, string>`` — so summing ``span.attributes["llm.cost"]``
        directly concatenates text instead of adding numbers.  Use this instead.
        """
        raw = self.attributes.get(key)
        if raw is None:
            return None
        try:
            value = float(raw)
            return value if math.isfinite(value) else None
        except (TypeError, ValueError):
            return None

    def attribute_int(self, key: str) -> int | None:
        """Return an attribute parsed as an int, or ``None`` if absent or unparseable."""
        raw = self.attributes.get(key)
        if raw is None:
            return None
        try:
            # Preserve integer precision instead of rounding through float.
            return int(raw)
        except (TypeError, ValueError):
            pass
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
    growing, and all take the same five arguments:

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
    ``quiet_for``
        Seconds the count must hold steady before the read returns, for a
        session that may still be running: an agent pauses between turns for
        longer than one poll. ``None``, the default, honors
        ``CHALK_TRACE_COMPLETE_AFTER_SECONDS`` when the function environment
        sets it -- that is how an environment's "trace is complete" setting
        reaches a scorer -- and otherwise returns as soon as the count holds
        across two polls. It cannot exceed ``timeout``.

    A steady count is a pragmatic signal, not a proof: it cannot tell "complete"
    from "quiet", so a long pause between agent turns can settle early. Pass
    ``quiet_for`` to require a longer silence, or ``expect_min`` when the caller
    knows better.
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
        quiet_for: float | None = None,
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
        wait, timeout, poll_interval, expect_min, quiet_for
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
            quiet_for=quiet_for,
        )

    def summary(
        self,
        *,
        wait: bool = True,
        timeout: float = 60.0,
        poll_interval: float = 2.0,
        expect_min: int | None = None,
        quiet_for: float | None = None,
    ) -> SessionSummary:
        """Return this session's aggregate: turn count, span count, wall clock.

        Settles on the same signal as :meth:`get_spans`, so ``trace_count`` and
        ``span_count`` do not undercount a session whose spans are still
        arriving.

        Parameters
        ----------
        wait, timeout, poll_interval, expect_min, quiet_for
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
                quiet_for=quiet_for,
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
        quiet_for: float | None = None,
    ) -> list[TraceSummary]:
        """Return a summary per trace in this session — typically one per turn.

        Settles on the same signal as :meth:`get_spans`: a turn that is still
        recording grows this session's span count without adding a trace, so
        waiting on the traces alone would return stale per-trace counts.

        Parameters
        ----------
        limit
            Stop after this many traces.  ``None`` returns all of them.
        wait, timeout, poll_interval, expect_min, quiet_for
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
            quiet_for=quiet_for,
        )

    def transcript(
        self,
        *,
        max_chars_per_turn: int = 4000,
        require_content: bool = False,
        wait: bool = True,
        timeout: float = 60.0,
        poll_interval: float = 2.0,
        expect_min: int | None = None,
        quiet_for: float | None = None,
    ) -> str:
        """Render this session's LLM turns as text, oldest first.

        A turn is a span carrying the OpenInference ``input.value`` attribute
        (the prompt) or Chalk's input-content metadata; ``output.value`` is its
        completion, when the span has one. Externally stored or omitted content
        renders as an explicit note: it is not an absent turn. Content URIs are
        neither fetched nor included in the text. Each turn is headed by the
        model and its token total. Prompts keep their *last*
        ``max_chars_per_turn`` characters and completions their
        *first*, so truncation preserves the newest context the model saw and
        the start of what it said. A session with no turns renders as a
        one-line note rather than an empty string.

        This is what :func:`chalkcompute.scorers.llm_judge` shows the model when
        a judge names ``trace`` among its inputs.

        Parameters
        ----------
        max_chars_per_turn
            Characters kept of each prompt and of each completion.
        require_content
            Raise :class:`TraceError` if a turn's raw prompt or completion is
            known to be unavailable or truncated, instead of rendering its
            omission note. The default ``llm_judge`` prompt enables this to
            avoid grading incomplete content. This does not disable the
            deliberate ``max_chars_per_turn`` display limit.
        wait, timeout, poll_interval, expect_min, quiet_for
            See :ref:`the settling rules <trace-waiting>` on :class:`Trace`.
            The wait settles on the whole session but needs a turn: it holds
            until at least one LLM turn is there and the session's span count
            -- every span, not only the turns -- stays steady, so a turn that
            is still being ingested is never mistaken for none, the root span
            that lands before any turn cannot end the wait early, and a tool
            call between turns keeps it open. A session that shows no turn
            within ``timeout`` renders as the no-turns note rather than
            raising, once an unwaited read confirms it, so a judge still grades
            the row; pass ``expect_min=0`` to accept an empty transcript
            without waiting.
        """
        client = self._require_client()

        def fetch() -> list[Span]:
            return client.list_spans(self.id)

        try:
            spans = _settle(
                fetch,
                spans_seen=len,
                target_seen=_count_turns,
                subject=f"session {self.id!r}",
                wait=wait,
                timeout=timeout,
                poll_interval=poll_interval,
                expect_min=expect_min,
                quiet_for=quiet_for,
            )
        except TimeoutError:
            # A session that has shown no turn after the wait has none to
            # render. One whose turns are still arriving must not be read
            # short: the note would pass for a real result.
            if _count_turns(fetch()):
                raise
            spans = []
        return _format_transcript(
            spans,
            max_chars_per_turn=max_chars_per_turn,
            require_content=require_content,
        )

    def _require_client(self) -> TraceClient:
        if self._client is None:
            object.__setattr__(self, "_client", TraceClient())
        client = self._client
        assert client is not None
        return client


def _format_transcript(
    spans: Sequence[Span], *, max_chars_per_turn: int, require_content: bool = False
) -> str:
    """Render the LLM turns among ``spans``; see :meth:`Trace.transcript`."""
    if max_chars_per_turn <= 0:
        raise ValueError("max_chars_per_turn must be positive")
    turns = sorted(
        (span for span in spans if _has_content(span, "input")),
        # Spans without a start time sort last, in the order they arrived.
        key=lambda span: (
            span.start_time is None,
            span.start_time.timestamp() if span.start_time is not None else 0.0,
        ),
    )
    if not turns:
        return _NO_TURNS_NOTE
    parts: list[str] = []
    for index, span in enumerate(turns, start=1):
        model = span.model or "unknown model"
        tokens = span.total_tokens
        heading = f"### turn {index} ({model}"
        heading += f", {tokens:,} tokens)" if tokens is not None else ")"
        lines = [heading]
        for direction, label in (("input", "prompt"), ("output", "completion")):
            content = _transcript_content(
                span,
                direction,
                max_chars=max_chars_per_turn,
                require_content=require_content,
            )
            if content is not None:
                lines += [f"[{label}]", content]
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _count_turns(spans: Sequence[Span]) -> int:
    """How many of ``spans`` a transcript renders: those carrying a prompt."""
    return sum(1 for span in spans if _has_content(span, "input"))


def _has_content(span: Span, direction: str) -> bool:
    """Metadata is evidence of content even when its body is not inline."""
    return f"{direction}.value" in span.attributes or any(
        f"chalk.trace.{direction}.{key}" in span.attributes for key in _CONTENT_METADATA
    )


def _transcript_content(
    span: Span, direction: str, *, max_chars: int, require_content: bool
) -> str | None:
    if not _has_content(span, direction):
        return None
    attributes = span.attributes
    prefix = f"chalk.trace.{direction}."
    value = attributes.get(f"{direction}.value")
    truncation = attributes.get(f"{prefix}truncation")
    raw_truncated = False
    if truncation is not None:
        # Router truncation can concern flattened metadata, while the full raw
        # body remains intact. Only raw_content proves that body was lost.
        try:
            details = json.loads(truncation)
        except (TypeError, ValueError):
            details = None
        raw_truncated = isinstance(details, dict) and "raw_content" in details

    if require_content and (value is None or raw_truncated):
        raise TraceError(
            f"Trace content is incomplete: {direction} for span {span.span_id!r} "
            "is not loaded or was omitted/truncated. Cannot score the full trace. "
            "Use transcript(require_content=False) to inspect the omission."
        )

    if value is not None:
        keep = _keep_tail if direction == "input" else _keep_head
        content = keep(value, max_chars)
    elif f"{prefix}content_uri" in attributes:
        content = "(content stored externally; not loaded)"
    elif f"{prefix}upload_omitted" in attributes:
        content = "(content unavailable; upload omitted)"
    else:
        content = "(content unavailable; body not recorded inline)"
    if truncation is not None:
        content += "\n(content or trace attributes were truncated during recording)"
    return content


def _keep_head(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n… [{len(text) - limit:,} more characters]"


def _keep_tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"… [{len(text) - limit:,} earlier characters]\n{text[-limit:]}"


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


# Seconds a session must go without new spans before a waited read treats it as
# complete. The function environment sets it from the environment's "trace is
# complete" setting; unset, a read settles as soon as its count holds across
# two polls.
TRACE_COMPLETE_AFTER_ENV_VAR = "CHALK_TRACE_COMPLETE_AFTER_SECONDS"


def _trace_complete_after(quiet_for: float | None) -> float | None:
    """``quiet_for`` as given, else the environment's setting, else ``None``."""
    if quiet_for is not None:
        if quiet_for < 0:
            raise ValueError("quiet_for must be >= 0 or None")
        return quiet_for
    raw = os.environ.get(TRACE_COMPLETE_AFTER_ENV_VAR, "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        raise ValueError(
            f"{TRACE_COMPLETE_AFTER_ENV_VAR} must be a number of seconds, not {raw!r}"
        ) from None
    if seconds < 0:
        raise ValueError(f"{TRACE_COMPLETE_AFTER_ENV_VAR} must be >= 0, not {raw!r}")
    return seconds


def _settle(
    fetch: Callable[[], _T],
    *,
    spans_seen: Callable[[_T], int],
    subject: str,
    wait: bool,
    timeout: float,
    poll_interval: float,
    expect_min: int | None,
    quiet_for: float | None = None,
    target_seen: Callable[[_T], int] | None = None,
) -> _T:
    """Poll ``fetch`` until the number of spans it can see stops growing.

    Every read of a session settles on the same signal, because every read is
    downstream of the same asynchronous ingest: spans, a session aggregate, and
    per-trace aggregates all grow as spans land. ``spans_seen`` extracts that
    one number from whatever ``fetch`` returns. ``target_seen`` is the count
    ``expect_min`` applies to when that is a narrower one -- a transcript waits
    on the whole session but needs a turn -- and defaults to ``spans_seen``.

    With ``quiet_for``, the count must also have held for that many seconds,
    measured from the read that last saw it change. A session is only ever
    observed through its spans, so this is the nearest thing to "no span has
    arrived for N seconds" a reader has.

    Returns the settled payload. Raises :class:`TimeoutError` rather than
    returning a knowingly incomplete one — an undercount reads as a real result.
    """
    if timeout < 0:
        raise ValueError("timeout must be >= 0")
    if poll_interval < 0:
        raise ValueError("poll_interval must be >= 0")
    if expect_min is not None and expect_min < 0:
        raise ValueError("expect_min must be >= 0 or None")
    if quiet_for is not None and quiet_for < 0:
        raise ValueError("quiet_for must be >= 0 or None")

    if not wait:
        return fetch()

    # Resolved only for a read that waits: an unwaited read never uses the
    # window, so the environment's setting cannot make it invalid.
    quiet_for = _trace_complete_after(quiet_for)
    if quiet_for is not None and quiet_for > timeout:
        raise ValueError(
            f"quiet_for ({quiet_for}s) exceeds timeout ({timeout}s), so the read "
            f"could never settle"
        )
    reached = target_seen or spans_seen
    target = 1 if expect_min is None else expect_min
    deadline = time.monotonic() + timeout
    result = fetch()
    count = spans_seen(result)
    previous = -1
    # When the count was last seen to change; the quiet window runs from here.
    steady_since = time.monotonic()
    while True:
        now = time.monotonic()
        steady = reached(result) >= target and count == previous
        quiet = now - steady_since
        if steady and (quiet_for is None or quiet >= quiet_for):
            return result
        remaining = deadline - now
        if remaining <= 0:
            if reached(result) < target:
                raise TimeoutError(
                    f"{subject} had {reached(result)} span(s) after {timeout}s, "
                    f"expected at least {target}; spans may still be in flight. "
                    f"Pass wait=False to accept a partial read."
                )
            if steady:
                raise TimeoutError(
                    f"{subject} held at {count} span(s) for {quiet:.0f}s of the "
                    f"{quiet_for}s quiet_for requires, within timeout={timeout}s. "
                    f"Raise timeout or pass wait=False."
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
        pause = min(poll_interval, remaining)
        if steady and quiet_for is not None:
            # Only the quiet window is outstanding: wake when it closes rather
            # than up to a poll late.
            pause = min(pause, quiet_for - quiet)
        previous = count
        time.sleep(pause)
        result = fetch()
        count = spans_seen(result)
        if count != previous:
            steady_since = time.monotonic()


def _session_query(session_id: str) -> str:
    return f"session:{json.dumps(session_id)}"


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
