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

    A caller-supplied ``x-chalk-session-id`` header additionally enters this
    row's OpenTelemetry context, so every span the row emits is grouped into
    that session -- in a trace of the row's own, since rows of one call share an
    invocation span and a trace carries at most one session. See
    :func:`chalkcompute._tracing.enter_session`.
    """
    resolved = _headers_from_context(context)
    return CallScopeToken(
        context=_CALL_CONTEXT.set(resolved),
        row_metadata=_ROW_METADATA.set(row_metadata),
        session=_enter_session(resolved),
    )


def reset_call_scope(token: CallScopeToken) -> None:
    """Restore whatever :func:`set_call_scope` replaced."""
    _CALL_CONTEXT.reset(token.context)
    _ROW_METADATA.reset(token.row_metadata)
    if token.session is not None:
        from chalkcompute._tracing import exit_session

        exit_session(token.session)


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


def _enter_session(context: Mapping[str, str]) -> object | None:
    session_id = _session_id_from_headers(context)
    if session_id is None:
        return None
    from chalkcompute._tracing import enter_session

    return enter_session(session_id)


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
