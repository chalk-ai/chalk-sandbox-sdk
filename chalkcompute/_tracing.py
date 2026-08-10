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
import importlib
import inspect
import os
from collections.abc import Callable, Iterator
from typing import Any, TypeVar, cast, overload

F = TypeVar("F", bound=Callable[..., Any])

_TRACER_NAME = "chalkcompute"
_DEFAULT_PUBLIC_SPAN_NAME = "chalkcompute.span"
_TRACE_CONTEXT_METADATA_KEYS = ("traceparent", "tracestate", "baggage")
_REMOTE_FUNCTION_TRACE_POLICY_ENV_VAR = "CHALK_REMOTE_FUNCTION_TRACE_POLICY"
_initialized = False
_provider: Any | None = None
_user_tracing_configured = False
_tracer: Any | None = None


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


@functools.cache
def _get_otel_module(module_name: str) -> Any | None:
    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None


def _get_context_api() -> Any | None:
    return _get_otel_module("opentelemetry.context")


def _get_propagate_api() -> Any | None:
    return _get_otel_module("opentelemetry.propagate")


def _get_trace_api() -> Any | None:
    return _get_otel_module("opentelemetry.trace")


def _require_trace_api() -> Any:
    trace_api = _get_trace_api()
    if trace_api is None:
        raise ImportError("opentelemetry.trace is required for tracing")
    return trace_api


def _has_tracing_intent() -> bool:
    # Internal SDK instrumentation should stay cold until Chalk tracing is
    # configured or OTEL exporter env vars indicate process-level tracing.
    return _provider is not None or _user_tracing_configured or _is_enabled()


def _install_tracer_provider(sample_rate: float) -> None:
    global _provider, _tracer
    if _provider is not None:
        return

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    trace_api = _require_trace_api()
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

    trace_api.set_tracer_provider(provider)
    _provider = provider
    _tracer = None
    atexit.register(provider.shutdown)


def _configure_tracing(*, sample_rate: float = 1.0) -> None:
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
        _configure_tracing(sample_rate=1.0)


def _is_remote_tracing_off() -> bool:
    return os.environ.get(_REMOTE_FUNCTION_TRACE_POLICY_ENV_VAR) == "always_off"


def get_tracer() -> Any:
    """Return a tracer for ``chalkcompute``.

    Returns a configured tracer if OTLP env vars are set, otherwise the
    default no-op tracer.

    Returns
    -------
    opentelemetry.trace.Tracer
        The configured or no-op tracer.
    """
    global _tracer
    _ensure_initialized()
    if _tracer is None:
        _tracer = _require_trace_api().get_tracer(_TRACER_NAME)
    return _tracer


def has_current_trace_context() -> bool:
    """Return whether the current context carries a valid trace."""
    trace_api = _get_trace_api()
    if trace_api is None:
        return False

    if not _is_remote_tracing_off():
        _ensure_initialized()
    span_context = trace_api.get_current_span().get_span_context()
    return bool(span_context.is_valid)


def current_trace_metadata() -> list[tuple[str, str]]:
    """Return W3C trace metadata for the current OpenTelemetry context."""
    if not has_current_trace_context():
        return []

    propagate_api = _get_propagate_api()
    if propagate_api is None:
        return []

    _ensure_initialized()
    carrier: dict[str, str] = {}
    propagate_api.inject(carrier)
    return [
        (key, value)
        for key, value in carrier.items()
        if key.lower() in _TRACE_CONTEXT_METADATA_KEYS and value
    ]


def has_trace_metadata(metadata: list[tuple[str, str]]) -> bool:
    return any(key.lower() in _TRACE_CONTEXT_METADATA_KEYS for key, _ in metadata)


@contextlib.contextmanager
def _span(name: str) -> Iterator[Any]:
    if _is_remote_tracing_off():
        yield None
        return

    _ensure_user_tracing_configured()

    with get_tracer().start_as_current_span(name) as span:
        yield span


def _default_span_name(func: Callable[..., Any]) -> str:
    return getattr(func, "__name__", _DEFAULT_PUBLIC_SPAN_NAME)


def _wrap_with_span(
    func: F,
    span_factory: Callable[[], contextlib.AbstractContextManager[Any]],
) -> F:
    """Wrap ``func`` so a span from ``span_factory`` stays open for its full execution.

    Coroutine functions and async generator functions must keep the span current
    while the coroutine is awaited or the generator is iterated, not just while
    the coroutine/generator object is constructed.
    """
    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with span_factory():
                return await func(*args, **kwargs)

        return cast(F, async_wrapper)

    if inspect.isasyncgenfunction(func):

        @functools.wraps(func)
        async def async_gen_wrapper(*args: Any, **kwargs: Any) -> Any:
            with span_factory():
                async for item in func(*args, **kwargs):
                    yield item

        return cast(F, async_gen_wrapper)

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        with span_factory():
            return func(*args, **kwargs)

    return cast(F, sync_wrapper)


class _SpanContextManager(contextlib.AbstractContextManager[Any]):
    """Dual-purpose context manager / decorator returned by ``span``."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._cm: contextlib.AbstractContextManager[Any] | None = None

    def __enter__(self) -> Any:
        cm = _span(self._name)
        self._cm = cm
        return cm.__enter__()

    def __exit__(self, *exc_info: Any) -> Any:
        cm = self._cm
        self._cm = None
        assert cm is not None
        return cm.__exit__(*exc_info)

    def __call__(self, func: F) -> F:
        return _wrap_with_span(func, lambda: _span(self._name))


def span(name: str = _DEFAULT_PUBLIC_SPAN_NAME) -> _SpanContextManager:
    """Start a span and propagate its trace context to nested calls.

    Usable as a context manager (``with chalkcompute.span(...):``) or as a
    decorator (``@chalkcompute.span(...)``), including on coroutine functions
    and async generator functions.
    """
    return _SpanContextManager(name)


def _decorate_traced(func: F, name: str | None) -> F:
    span_name = name or _default_span_name(func)
    return _wrap_with_span(func, lambda: _span(span_name))


def _instrumented(name: str) -> Callable[[F], F]:
    """Trace internal SDK work without treating it as user tracing intent."""

    def decorator(func: F) -> F:
        def span_factory() -> contextlib.AbstractContextManager[Any]:
            if _is_remote_tracing_off() or not _has_tracing_intent():
                return contextlib.nullcontext()
            return get_tracer().start_as_current_span(name)

        return _wrap_with_span(func, span_factory)

    return decorator


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

    if isinstance(arg, str) and not arg.strip():
        raise ValueError("span name cannot be empty")

    def decorator(func: F) -> F:
        return _decorate_traced(func, arg)

    return decorator


@contextlib.contextmanager
def remote_function_call_span(function_name: str, call_type: str) -> Iterator[Any]:
    """Trace one outbound call from this process to another compute function."""
    if _is_remote_tracing_off() or not has_current_trace_context():
        yield None
        return

    otel_context = _get_context_api()
    trace_api = _get_trace_api()
    if otel_context is None or trace_api is None:
        yield None
        return

    parent_context = otel_context.get_current()
    with get_tracer().start_as_current_span(
        "chalkcompute.remote_function.call",
        kind=trace_api.SpanKind.CLIENT,
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
        yield span
