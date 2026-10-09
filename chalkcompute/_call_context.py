"""What the caller attached to the invocation in progress, readable without declaring it.

A remote call carries two side channels beside the function's arguments, and neither is
something a function should have to add a parameter for:

* **Call context** — the request's gRPC headers, which the runtime hands the handler inside
  its ``context`` argument. Per request, so it is the same for every row of one call. Chalk
  sets these from ``catalog_call('fn', ..., metadata => {'x-chalk-session-id': session_id})``,
  and the SDK's own client already puts ``traceparent`` and ``x-chalk-request-timestamp``
  here.
* **Row metadata** — an opaque blob attached to each row, from
  ``catalog_call('fn', ..., row_metadata => <binary expr>)``. Per row, so it cannot be a
  header: it travels in the request body under a reserved column that the handler shim
  strips out of the argument mapping.

The blob stays opaque with one exception: when it parses as a UTF-8 JSON object naming a
``__chalk__session_id__``, that id places the row's spans in that session, exactly as the
header does for the whole request. A batch whose rows belong to different conversations has
no other way to say so, and making the function parse the blob itself would leave every
caller reinventing the same three lines.

Both are optional in both directions. A caller may send neither, one, or both; a function
may read neither, one, or both. Reading one the caller did not send yields an empty mapping
or ``None`` rather than an error.

Both are published as context variables, so they are correct under the shim's thread pool
(batched calls run rows concurrently) and under asyncio: each worker gets its own copy.

    import chalkcompute

    @chalkcompute.function()
    def score(x: int) -> int:
        session = chalkcompute.get_call_context().get("x-chalk-session-id")
        blob = chalkcompute.get_row_metadata()
        ...
"""

from __future__ import annotations

import functools
import json
from contextvars import ContextVar, Token
from types import MappingProxyType
from typing import Any, Mapping, NamedTuple

__all__ = [
    "get_call_context",
    "get_row_metadata",
    "set_call_scope",
    "reset_call_scope",
]

_EMPTY_CONTEXT: Mapping[str, str] = MappingProxyType({})

_CALL_CONTEXT: ContextVar[Mapping[str, str]] = ContextVar(
    "chalk_call_context", default=_EMPTY_CONTEXT
)
_ROW_METADATA: ContextVar[bytes | None] = ContextVar("chalk_row_metadata", default=None)


class CallScopeToken(NamedTuple):
    """Undo tokens for what :func:`set_call_scope` set."""

    context: Token[Mapping[str, str]]
    row_metadata: Token[bytes | None]
    session: object | None = None
    """OpenTelemetry session scope, when the caller sent a session id."""


def get_call_context() -> Mapping[str, str]:
    """The gRPC headers of the request being served, as a read-only mapping.

    Returns
    -------
    Mapping[str, str]
        The caller's headers, empty when the caller sent none or when the function was
        invoked outside a Chalk remote call. Never ``None``, so ``.get(...)`` is always safe.
    """
    return _CALL_CONTEXT.get()


def get_row_metadata() -> bytes | None:
    """The caller-supplied metadata for the row being processed, or ``None`` if there is none.

    Returns
    -------
    bytes | None
        The blob the caller attached to this row via ``row_metadata =>``. ``None`` when the
        caller sent none, when the column was null for this row, or when the function was
        invoked outside a Chalk remote call.
    """
    return _ROW_METADATA.get()


def set_call_scope(
    context: Mapping[str, Any] | None,
    row_metadata: bytes | None,
) -> CallScopeToken:
    """Publish one row's call context and metadata. Returns a token for :func:`reset_call_scope`.

    Called by the generated handler shims around each row, with the ``context`` argument the
    runtime gave them; user code reads :func:`get_call_context` and :func:`get_row_metadata`
    instead.

    A session id additionally enters this row's OpenTelemetry context, so every span the row
    emits is grouped into that session -- in a trace of the row's own, since rows of one call
    share an invocation span and a trace carries at most one session. It is taken from the
    row's ``__chalk__session_id__``, falling back to the ``x-chalk-session-id`` header: the
    row is the more specific of the two, and the header cannot disagree between rows of one
    request anyway. See :func:`chalkcompute._tracing.enter_session`.
    """
    resolved = _headers_from_context(context)
    return CallScopeToken(
        context=_CALL_CONTEXT.set(resolved),
        row_metadata=_ROW_METADATA.set(row_metadata),
        session=_enter_session(resolved, row_metadata),
    )


def reset_call_scope(token: CallScopeToken) -> None:
    """Restore whatever :func:`set_call_scope` replaced."""
    _CALL_CONTEXT.reset(token.context)
    _ROW_METADATA.reset(token.row_metadata)
    if token.session is not None:
        from chalkcompute._tracing import exit_session, force_flush_tracing

        exit_session(token.session)
        if not force_flush_tracing():
            raise RuntimeError(
                "the OpenTelemetry provider did not flush this session's spans"
            )


def _headers_from_context(context: Mapping[str, Any] | None) -> Mapping[str, str]:
    """Pull the request's headers out of the runtime's ``context`` argument.

    ``chalk_remote_call`` hands the handler an envelope -- ``function_name``, ``peer``, and
    the headers nested under ``metadata`` -- so publishing the envelope itself would leave
    every header one level out of reach of :func:`get_call_context`. A flat mapping is taken
    as the headers themselves, which is what direct callers and tests pass.
    """
    if not context:
        return _EMPTY_CONTEXT
    headers = context.get("metadata") if "metadata" in context else context
    if not isinstance(headers, Mapping) or not headers:
        return _EMPTY_CONTEXT
    return MappingProxyType({k: v for k, v in headers.items() if isinstance(v, str)})


def _enter_session(
    context: Mapping[str, str], row_metadata: bytes | None
) -> object | None:
    session_id = _session_id_from_row_metadata(
        row_metadata
    ) or _session_id_from_headers(context)
    if session_id is None:
        return None
    from chalkcompute._tracing import enter_session

    return enter_session(session_id)


def _session_id_from_row_metadata(row_metadata: bytes | None) -> str | None:
    """Read ``__chalk__session_id__`` out of a row's blob, if the blob is JSON saying so.

    The blob belongs to the caller and may be anything -- protobuf, msgpack, a raw
    identifier -- so every step here is a question, not an assumption, and any answer of
    "no" leaves the row to the header. Callers see a bad blob as an absent session rather
    than a failed row: this runs per row on the request path, and a malformed side channel
    must not be able to fail a call that would otherwise succeed.

    The substring test short-circuits the common cases without decoding: a blob that does
    not contain the key cannot yield one, and a non-JSON blob is rejected in a bytes scan
    rather than a UTF-8 decode plus a parse of something that was never JSON.
    """
    key, quoted = _session_id_key()
    if not row_metadata or quoted not in row_metadata:
        return None
    try:
        parsed = json.loads(row_metadata.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    value = parsed.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


@functools.cache
def _session_id_key() -> tuple[str, bytes]:
    """The row-metadata session key, and its spelling as a JSON object key.

    Quoted for the scan so it cannot match a *value* that happens to read like the key.
    Derived from the one constant rather than written out again, and cached because the
    import stays lazy while this runs per row.
    """
    from chalkcompute._tracing import SESSION_ID_ROW_METADATA_KEY

    return SESSION_ID_ROW_METADATA_KEY, f'"{SESSION_ID_ROW_METADATA_KEY}"'.encode()


def _session_id_from_headers(context: Mapping[str, str]) -> str | None:
    """Find the session header case-insensitively, as gRPC metadata allows."""
    from chalkcompute._tracing import SESSION_ID_HEADER

    for key, value in context.items():
        if (
            key.lower() == SESSION_ID_HEADER
            and isinstance(value, str)
            and value.strip()
        ):
            return value.strip()
    return None
