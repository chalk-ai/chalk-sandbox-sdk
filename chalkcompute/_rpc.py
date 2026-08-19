"""Shared ConnectRPC client instrumentation."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from connectrpc.errors import ConnectError
from connectrpc.protocol import ProtocolType
from connectrpc.request import RequestContext
from opentelemetry.trace import Span, StatusCode

from chalkcompute._tracing import get_tracer

_RPC_SPAN_NAME = "chalkcompute.rpc"
_RPC_SYSTEM_NAME_ATTRIBUTE = "rpc.system.name"
_RPC_METHOD_ATTRIBUTE = "rpc.method"
_RPC_RESPONSE_STATUS_CODE_ATTRIBUTE = "rpc.response.status_code"


@dataclass
class _SpanToken:
    context: AbstractContextManager[Span]
    span: Span


class TracingInterceptor:
    """Emit one OpenTelemetry span around each synchronous RPC."""

    def __init__(self, protocol: ProtocolType) -> None:
        self._protocol = protocol

    def on_start_sync(self, ctx: RequestContext[Any, Any]) -> _SpanToken:
        context = get_tracer().start_as_current_span(_RPC_SPAN_NAME)
        span = context.__enter__()
        span.set_attribute(_RPC_SYSTEM_NAME_ATTRIBUTE, self._protocol.name.lower())
        span.set_attribute(
            _RPC_METHOD_ATTRIBUTE,
            f"{ctx.method.service_name}/{ctx.method.name}",
        )
        return _SpanToken(context=context, span=span)

    def on_end_sync(
        self,
        token: _SpanToken,
        ctx: RequestContext[Any, Any],
        error: Exception | None,
    ) -> None:
        del ctx
        status = (
            "OK"
            if error is None
            else error.code.name
            if isinstance(error, ConnectError)
            else "UNKNOWN"
        )
        token.span.set_attribute(_RPC_RESPONSE_STATUS_CODE_ATTRIBUTE, status)
        if error is not None:
            token.span.set_status(StatusCode.ERROR, status)
        token.context.__exit__(
            type(error) if error is not None else None,
            error,
            error.__traceback__ if error is not None else None,
        )


def rpc_interceptors(protocol: ProtocolType) -> tuple[TracingInterceptor]:
    """Return the common interceptors for a generated synchronous client."""
    return (TracingInterceptor(protocol),)
