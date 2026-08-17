"""What the caller attached to the invocation in progress, readable without declaring it.

A remote call carries two side channels beside the function's arguments, and neither is
something a function should have to add a parameter for:

* **Call context** — the request's gRPC headers, which the runtime hands the handler as its
  ``context`` argument. Per request, so it is the same for every row of one call. Chalk sets
  these from ``catalog_call('fn', ..., metadata => {'x-chalk-session-id': session_id})``,
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
from typing import Mapping, NamedTuple

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
    """Undo tokens for the two variables :func:`set_call_scope` sets."""

    context: Token[Mapping[str, str]]
    row_metadata: Token[bytes | None]


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
    context: Mapping[str, str] | None,
    row_metadata: bytes | None,
) -> CallScopeToken:
    """Publish one row's call context and metadata. Returns a token for :func:`reset_call_scope`.

    Called by the generated handler shims around each row; user code reads
    :func:`get_call_context` and :func:`get_row_metadata` instead.
    """
    return CallScopeToken(
        context=_CALL_CONTEXT.set(
            _EMPTY_CONTEXT if not context else MappingProxyType(dict(context))
        ),
        row_metadata=_ROW_METADATA.set(row_metadata),
    )


def reset_call_scope(token: CallScopeToken) -> None:
    """Restore whatever :func:`set_call_scope` replaced."""
    _CALL_CONTEXT.reset(token.context)
    _ROW_METADATA.reset(token.row_metadata)
