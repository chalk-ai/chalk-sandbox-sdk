"""Shared ConnectRPC client instrumentation."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, TypeVar

from connectrpc.code import Code
from connectrpc.errors import ConnectError
from connectrpc.request import RequestContext
from opentelemetry import propagate
from opentelemetry.trace import Span, StatusCode

from chalkcompute._tracing import current_trace_metadata, get_tracer

logger = logging.getLogger(__name__)

_RPC_SPAN_NAME = "chalkcompute.rpc"
_RPC_METHOD_ATTRIBUTE = "rpc.method"
_RPC_RESPONSE_STATUS_CODE_ATTRIBUTE = "rpc.response.status_code"
_RPC_TRACING_ENABLED: ContextVar[bool] = ContextVar(
    "chalkcompute_rpc_tracing_enabled", default=True
)

# Match the historical grpcio max receive message size (100MB). Connect only
# enforces a read limit; there is no client-side send-size knob.
RPC_READ_MAX_BYTES = 100 << 20

# Match the historical grpcio service_config retryPolicy for UNAVAILABLE.
RPC_RETRY_MAX_ATTEMPTS = 5
RPC_RETRY_INITIAL_BACKOFF_SECONDS = 0.1
RPC_RETRY_MAX_BACKOFF_SECONDS = 1.0
RPC_RETRY_BACKOFF_MULTIPLIER = 2.0

_REQ = TypeVar("_REQ")
_RES = TypeVar("_RES")


@dataclass
class _SpanToken:
    context: AbstractContextManager[Span]
    span: Span


@contextmanager
def suppress_rpc_tracing() -> Iterator[None]:
    """Disable client spans and trace propagation for RPCs in this context."""
    token = _RPC_TRACING_ENABLED.set(False)
    try:
        yield
    finally:
        _RPC_TRACING_ENABLED.reset(token)


class TracingInterceptor:
    """Emit one OpenTelemetry span around each synchronous RPC.

    The span is also handed to the server as W3C ``traceparent``/``tracestate``/
    ``baggage`` request headers, so the server's spans — and, through the
    sandbox's per-exec ``TRACEPARENT`` env, the workload's — join the caller's
    trace. The propagator writes nothing for a non-recording span, so a process
    without tracing sends no extra headers.
    """

    def on_start_sync(self, ctx: RequestContext[Any, Any]) -> _SpanToken | None:
        if not _RPC_TRACING_ENABLED.get():
            return None
        context = get_tracer().start_as_current_span(_RPC_SPAN_NAME)
        span = context.__enter__()
        # The RPC span is now current, so downstream services join it rather
        # than becoming disconnected roots. Trace headers win over caller input.
        for key, value in current_trace_metadata():
            ctx.request_headers[key] = value
        span.set_attribute(
            _RPC_METHOD_ATTRIBUTE,
            f"{ctx.method.service_name}/{ctx.method.name}",
        )
        carrier: dict[str, str] = {}
        propagate.inject(carrier)
        for key, value in carrier.items():
            ctx.request_headers[key] = value
        return _SpanToken(context=context, span=span)

    def on_end_sync(
        self,
        token: _SpanToken | None,
        ctx: RequestContext[Any, Any],
        error: Exception | None,
    ) -> None:
        del ctx
        if token is None:
            return
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


class RetryInterceptor:
    """Retry unary RPCs that fail with ``UNAVAILABLE``.

    Mirrors the historical grpcio ``service_config`` retry policy: five total
    attempts, exponential backoff from 0.1s to 1s with multiplier 2.
    """

    def intercept_unary_sync(
        self,
        call_next: Callable[[_REQ, RequestContext[_REQ, _RES]], _RES],
        request: _REQ,
        ctx: RequestContext[_REQ, _RES],
        /,
    ) -> _RES:
        # SQL execution can submit a job or write via COPY. A lost response
        # does not mean it is safe to execute the statement again. SQLJob.wait
        # owns poll retries so their time counts toward its waiting deadline.
        if (
            ctx.method.service_name == "chalk.protosql.v1.SqlService"
            and ctx.method.name in {"ExecuteSqlQuery", "PollSqlQuery"}
        ):
            return call_next(request, ctx)
        delay = RPC_RETRY_INITIAL_BACKOFF_SECONDS
        last_error: ConnectError | None = None
        for attempt in range(1, RPC_RETRY_MAX_ATTEMPTS + 1):
            try:
                return call_next(request, ctx)
            except ConnectError as exc:
                last_error = exc
                if exc.code != Code.UNAVAILABLE or attempt >= RPC_RETRY_MAX_ATTEMPTS:
                    raise
                logger.warning(
                    "%s/%s UNAVAILABLE (attempt %d/%d); retrying in %.1fs",
                    ctx.method.service_name,
                    ctx.method.name,
                    attempt,
                    RPC_RETRY_MAX_ATTEMPTS,
                    delay,
                )
                time.sleep(delay)
                delay = min(
                    delay * RPC_RETRY_BACKOFF_MULTIPLIER,
                    RPC_RETRY_MAX_BACKOFF_SECONDS,
                )
        assert last_error is not None
        raise last_error


def rpc_interceptors() -> tuple[RetryInterceptor, TracingInterceptor]:
    """Return the common interceptors for a generated synchronous client.

    Retry runs outermost so each attempt is traced independently.
    """
    return (RetryInterceptor(), TracingInterceptor())
