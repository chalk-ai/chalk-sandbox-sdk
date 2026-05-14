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
from collections.abc import Callable, Iterator
from typing import Any, TypeVar, cast, overload

F = TypeVar("F", bound=Callable[..., Any])

_TRACER_NAME = "chalkcompute"
_DEFAULT_PUBLIC_TRACE_NAME = "chalkcompute.trace"
_TRACE_CONTEXT_METADATA_KEYS = ("traceparent", "tracestate", "baggage")
_REMOTE_FUNCTION_TRACE_POLICY_ENV_VAR = "CHALK_REMOTE_FUNCTION_TRACE_POLICY"
_TRACE_SUPPRESSED_METADATA_KEY = "x-chalk-tracing-suppressed"
_initialized = False
_provider: Any | None = None
_user_tracing_configured = False


def _is_enabled() -> bool:
    return bool(
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    )


def _validate_sample_rate(sample_rate: float) -> float:
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int | float):
        raise TypeError("sample_rate must be a number between 0.0 and 1.0")
    rate = float(sample_rate)
    if rate < 0.0 or rate > 1.0:
        raise ValueError("sample_rate must be between 0.0 and 1.0")
    return rate


def _install_tracer_provider(sample_rate: float) -> None:
    global _provider
    if _provider is not None:
        return

    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    resource = Resource.create({"service.name": "chalkcompute"})
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(sample_rate)),
    )

    if _is_enabled():
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))

    trace.set_tracer_provider(provider)
    _provider = provider
    atexit.register(provider.shutdown)


def configure_tracing(*, sample_rate: float = 1.0) -> None:
    """Configure caller-side tracing for this process.

    The sampler is parent-based: existing parent trace decisions are respected,
    and ``sample_rate`` is only used when this process starts a new root trace.
    """
    global _initialized, _user_tracing_configured
    rate = _validate_sample_rate(sample_rate)
    _install_tracer_provider(rate)
    _initialized = True
    _user_tracing_configured = True


def _ensure_initialized() -> None:
    global _initialized
    if _initialized:
        return
    _initialized = True

    if not _is_enabled():
        return

    _install_tracer_provider(sample_rate=1.0)


def _ensure_user_tracing_configured() -> None:
    if not _user_tracing_configured:
        configure_tracing(sample_rate=1.0)


def _is_remote_tracing_suppressed() -> bool:
    return os.environ.get(_REMOTE_FUNCTION_TRACE_POLICY_ENV_VAR) == "off"


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
    if _is_remote_tracing_suppressed():
        return [(_TRACE_SUPPRESSED_METADATA_KEY, "1")]

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


@contextlib.contextmanager
def _span(name: str) -> Iterator[Any]:
    if _is_remote_tracing_suppressed():
        yield None
        return

    _ensure_user_tracing_configured()

    from opentelemetry.trace import Status, StatusCode

    with get_tracer().start_as_current_span(name) as span:
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


@contextlib.contextmanager
def trace(name: str = _DEFAULT_PUBLIC_TRACE_NAME) -> Iterator[Any]:
    """Start a caller-side span and propagate its trace context to nested calls."""
    with _span(name) as span:
        yield span


def _default_span_name(func: Callable[..., Any]) -> str:
    return getattr(func, "__name__", _DEFAULT_PUBLIC_TRACE_NAME)


def _decorate_traced(func: F, name: str | None) -> F:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with _span(name or _default_span_name(func)):
            return func(*args, **kwargs)

    return wrapper  # ty: ignore[invalid-return-type]


@overload
def traced(arg: F, /) -> F: ...


@overload
def traced(arg: str | None = None, /) -> Callable[[F], F]: ...


def traced(arg: str | F | None = None) -> Callable[[F], F] | F:
    """Trace a caller function.

    Supports both ``@chalkcompute.traced`` and
    ``@chalkcompute.traced("span_name")``. Using this decorator implies tracing
    intent; if tracing has not been configured, roots sample at 1.0.
    """
    if arg is not None and not isinstance(arg, str):
        if callable(arg):
            return _decorate_traced(cast(F, arg), None)
        raise TypeError("traced expects a callable, a span name, or no argument")

    def decorator(func: F) -> F:
        return _decorate_traced(func, arg)

    return decorator


@contextlib.contextmanager
def remote_function_call_span(function_name: str, call_type: str) -> Iterator[Any]:
    """Trace one outbound call from this process to another compute function."""
    if _is_remote_tracing_suppressed() or not has_current_trace_context():
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
