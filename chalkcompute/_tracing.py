"""OpenTelemetry tracing for chalkcompute.

Tracing is enabled only when OTEL_EXPORTER_OTLP_ENDPOINT or
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT is set in the environment.
When neither is present, all tracing operations are no-ops.
"""

from __future__ import annotations

import atexit
import functools
import os
from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

_TRACER_NAME = "chalkcompute"
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
    """Return a tracer for chalkcompute.

    Returns a configured tracer if OTLP env vars are set,
    otherwise returns the default no-op tracer.
    """
    from opentelemetry import trace

    _ensure_initialized()
    return trace.get_tracer(_TRACER_NAME)


def traced(name: str) -> Callable[[F], F]:
    """Decorator that wraps a function in an OpenTelemetry span."""

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with get_tracer().start_as_current_span(name):
                return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator
