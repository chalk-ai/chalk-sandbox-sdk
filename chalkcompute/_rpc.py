"""Shared ConnectRPC client instrumentation."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, TypeVar

from connectrpc.code import Code
from connectrpc.codec import proto_binary_codec
from connectrpc.errors import ConnectError
from connectrpc.request import RequestContext
from opentelemetry import propagate
from opentelemetry.trace import Span, StatusCode

from chalkcompute._tracing import get_tracer

logger = logging.getLogger(__name__)

_RPC_SPAN_NAME = "chalkcompute.rpc"
_RPC_METHOD_ATTRIBUTE = "rpc.method"
_RPC_RESPONSE_STATUS_CODE_ATTRIBUTE = "rpc.response.status_code"

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


class TracingInterceptor:
    """Emit one OpenTelemetry span around each synchronous RPC.

    The span is also handed to the server as W3C ``traceparent``/``tracestate``/
    ``baggage`` request headers, so the server's spans — and, through the
    sandbox's per-exec ``TRACEPARENT`` env, the workload's — join the caller's
    trace. The propagator writes nothing for a non-recording span, so a process
    without tracing sends no extra headers.
    """

    def on_start_sync(self, ctx: RequestContext[Any, Any]) -> _SpanToken:
        context = get_tracer().start_as_current_span(_RPC_SPAN_NAME)
        span = context.__enter__()
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
        # Invocation retries belong to the native client. Replaying an RPC
        # after a lost response could execute the handler beyond its budget.
        if (
            ctx.method.service_name
            == "chalk.externalfunctioncatalog.v1.ExternalFunctionCatalogService"
            and ctx.method.name == "CallExternalFunction"
        ):
            return call_next(request, ctx)
        # SQL execution can submit a job or write via COPY. A lost response
        # does not mean it is safe to execute the statement again. SQLJob.wait
        # owns poll retries so their time counts toward its waiting deadline.
        if (
            ctx.method.service_name == "chalk.protosql.v1.SqlService"
            and ctx.method.name in {"ExecuteSqlQuery", "PollSqlQuery"}
        ):
            return call_next(request, ctx)
        # Router creates have no idempotency key. A lost response must not
        # replay a successful creation. Reconcile by name/ID before retrying.
        if ctx.method.service_name.startswith(
            "chalk.router.v1."
        ) and ctx.method.name.startswith("Create"):
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


class RequestErrorInterceptor:
    """Raise request-side errors that the HTTP/2 transport hides.

    connectrpc encodes request messages inside the generator it hands to the
    HTTP/2 transport as the request body (unary requests too, under the gRPC
    protocol). If producing or encoding a request raises there, hyper swallows
    the exception, resets the stream, and the caller only sees
    ``ConnectError(UNAVAILABLE): ... stream error sent by user`` -- a code
    that also looks transient, so it gets retried. This interceptor finds the
    request-side error in a frame it can see and raises it in place of that
    ``ConnectError``.
    """

    def intercept_unary_sync(
        self,
        call_next: Callable[[_REQ, RequestContext[_REQ, _RES]], _RES],
        request: _REQ,
        ctx: RequestContext[_REQ, _RES],
        /,
    ) -> _RES:
        try:
            return call_next(request, ctx)
        except ConnectError as exc:
            _raise_encode_error(request, exc)
            raise

    def intercept_server_stream_sync(
        self,
        call_next: Callable[[_REQ, RequestContext[_REQ, _RES]], Iterator[_RES]],
        request: _REQ,
        ctx: RequestContext[_REQ, _RES],
        /,
    ) -> Iterator[_RES]:
        try:
            yield from call_next(request, ctx)
        except ConnectError as exc:
            _raise_encode_error(request, exc)
            raise

    def intercept_client_stream_sync(
        self,
        call_next: Callable[[Iterator[_REQ], RequestContext[_REQ, _RES]], _RES],
        request: Iterator[_REQ],
        ctx: RequestContext[_REQ, _RES],
        /,
    ) -> _RES:
        state = _StreamState()
        try:
            return call_next(_tracked_requests(request, state), ctx)
        except ConnectError as exc:
            state.raise_request_error(exc)
            raise

    def intercept_bidi_stream_sync(
        self,
        call_next: Callable[
            [Iterator[_REQ], RequestContext[_REQ, _RES]], Iterator[_RES]
        ],
        request: Iterator[_REQ],
        ctx: RequestContext[_REQ, _RES],
        /,
    ) -> Iterator[_RES]:
        state = _StreamState()
        try:
            yield from call_next(_tracked_requests(request, state), ctx)
        except ConnectError as exc:
            state.raise_request_error(exc)
            raise


def _raise_encode_error(request: object, exc: ConnectError) -> None:
    # Only reached after a failed call, so successful calls never pay for a
    # second encode.
    try:
        proto_binary_codec().encode(request)
    except Exception as request_error:
        raise request_error from exc


class _StreamState:
    """Per-call state for a request stream: the last message handed to
    connectrpc and any exception the caller's request iterator raised."""

    def __init__(self) -> None:
        self.last: object | None = None
        self.error: Exception | None = None

    def raise_request_error(self, exc: ConnectError) -> None:
        if self.error is not None:
            raise self.error from exc
        if self.last is not None:
            # connectrpc encodes each message right after pulling it, so a
            # failed encode is always of the last message yielded. Only
            # re-encoded after a failed call: a successful stream is never
            # encoded twice.
            _raise_encode_error(self.last, exc)


def _tracked_requests(requests: Iterable[_REQ], state: _StreamState) -> Iterator[_REQ]:
    iterator = iter(requests)
    while True:
        try:
            request = next(iterator)
        except StopIteration:
            return
        except Exception as exc:
            state.error = exc
            raise
        state.last = request
        yield request


def rpc_interceptors() -> tuple[
    RetryInterceptor, TracingInterceptor, RequestErrorInterceptor
]:
    """Return the common interceptors for a generated synchronous client.

    Retry runs outermost so each attempt is traced independently. Request
    errors are surfaced innermost, so tracing records the real error and
    retry never replays it (it is not a ``ConnectError``).
    """
    return (RetryInterceptor(), TracingInterceptor(), RequestErrorInterceptor())
