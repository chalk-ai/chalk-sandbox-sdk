"""OpenTelemetry tracing for chalkcompute.

Span export is configured when OTEL_EXPORTER_OTLP_ENDPOINT or
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT is set in the environment. When neither is
present and no application tracer provider is installed, tracing operations use
OpenTelemetry's no-op provider.
"""

from __future__ import annotations

import atexit
import contextlib
import functools
import os
from collections.abc import Callable, Iterator, Mapping
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

_TRACER_NAME = "chalkcompute"
_TRACE_CONTEXT_METADATA_KEYS = ("traceparent", "tracestate", "baggage")
_initialized = False


def _is_enabled() -> bool:
    return bool(
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    )


def _ensure_initialized() -> None:
    global _initialized
    if _initialized:
        return
    _initialized = True

    if not _is_enabled():
        return

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": "chalkcompute"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    atexit.register(provider.shutdown)


def get_tracer() -> Any:
    """Return a tracer for ``chalkcompute``.

    Returns a configured tracer if OTLP env vars are set, otherwise the
    default no-op tracer.

    Returns
    -------
    opentelemetry.trace.Tracer
        The configured or no-op tracer.
    """
    from opentelemetry import trace

    _ensure_initialized()
    return trace.get_tracer(_TRACER_NAME)


def has_current_trace_context() -> bool:
    """Return whether the current context carries a valid trace."""
    try:
        from opentelemetry import trace
    except ImportError:
        return False

    _ensure_initialized()
    span_context = trace.get_current_span().get_span_context()
    return bool(span_context.is_valid)


def current_trace_metadata() -> list[tuple[str, str]]:
    """Return W3C trace metadata for the current OpenTelemetry context."""
    if not has_current_trace_context():
        return []

    try:
        from opentelemetry import propagate
    except ImportError:
        return []

    _ensure_initialized()
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return [
        (key, value)
        for key, value in carrier.items()
        if key.lower() in _TRACE_CONTEXT_METADATA_KEYS and value
    ]


def has_trace_metadata(metadata: list[tuple[str, str]]) -> bool:
    return any(key.lower() in _TRACE_CONTEXT_METADATA_KEYS for key, _ in metadata)


def _extract_remote_call_context(context_metadata: Any) -> Any | None:
    if not isinstance(context_metadata, Mapping):
        return None

    raw_metadata = context_metadata.get("metadata", context_metadata)
    if not isinstance(raw_metadata, Mapping):
        return None

    carrier = {
        str(key).lower(): str(value)
        for key, value in raw_metadata.items()
        if str(key).lower() in _TRACE_CONTEXT_METADATA_KEYS and value
    }
    if not carrier:
        return None

    try:
        from opentelemetry import propagate
    except ImportError:
        return None

    return propagate.extract(carrier)


@contextlib.contextmanager
def remote_function_call_span(function_name: str, call_type: str) -> Iterator[Any]:
    """Trace one outbound call from this process to another compute function."""
    if not has_current_trace_context():
        yield None
        return

    try:
        from opentelemetry import context as otel_context
        from opentelemetry.trace import SpanKind, Status, StatusCode
    except ImportError:
        yield None
        return

    parent_context = otel_context.get_current()
    with get_tracer().start_as_current_span(
        "chalkcompute.remote_function.call",
        kind=SpanKind.CLIENT,
    ) as span:
        span.set_attribute("chalk.remote_function.name", function_name)
        span.set_attribute("chalk.remote_function.call_type", call_type)
        if not span.get_span_context().is_valid:
            token = otel_context.attach(parent_context)
            try:
                yield span
            finally:
                otel_context.detach(token)
            return
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


@contextlib.contextmanager
def remote_function_invocation_span(function_name: str, context_metadata: Any) -> Iterator[Any]:
    """Trace a compute function invocation and restore propagated context."""
    parent_context = _extract_remote_call_context(context_metadata)
    if parent_context is None:
        yield None
        return

    from opentelemetry import context as otel_context
    from opentelemetry.trace import SpanKind, Status, StatusCode

    with get_tracer().start_as_current_span(
        "chalkcompute.remote_function.invoke",
        context=parent_context,
        kind=SpanKind.SERVER,
    ) as span:
        span.set_attribute("chalk.remote_function.name", function_name)
        if not span.get_span_context().is_valid:
            token = otel_context.attach(parent_context)
            try:
                yield span
            finally:
                otel_context.detach(token)
            return
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


def traced(name: str) -> Callable[[F], F]:
    """Wrap a function in an OpenTelemetry span.

    Parameters
    ----------
    name
        Span name.

    Returns
    -------
    Callable
        A decorator that wraps the target callable.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with get_tracer().start_as_current_span(name):
                return func(*args, **kwargs)

        return wrapper  # ty: ignore[invalid-return-type]

    return decorator
