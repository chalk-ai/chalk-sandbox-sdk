"""Configure the SDK-owned exporter without changing application logging."""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

logger = logging.getLogger(__name__)


def _grpc_endpoint() -> str:
    return (
        os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or "http://localhost:4317"
    )


def create_span_exporter() -> SpanExporter:
    protocol = (
        os.environ.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL")
        or os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL")
        or "http/protobuf"
    )
    if protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        channel_options = (
            (("grpc.default_authority", "localhost"),)
            if _grpc_endpoint().startswith("unix:")
            else None
        )
        return _DiagnosticExporter(OTLPSpanExporter(channel_options=channel_options))
    elif protocol == "http/protobuf":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
    else:
        raise ValueError(f"Unsupported OTLP trace protocol: {protocol!r}")
    return _DiagnosticExporter(OTLPSpanExporter())


class _DiagnosticExporter(SpanExporter):
    """Report an outage once, resetting after a successful export."""

    def __init__(self, exporter: SpanExporter) -> None:
        self._exporter = exporter
        self._failed = False
        self._filter = _ExportLogFilter()
        modules = {type(exporter).__module__}
        modules.update(
            cls.__module__
            for cls in type(exporter).__mro__
            if cls.__module__.startswith("opentelemetry.exporter.otlp.")
        )
        self._loggers = [logging.getLogger(module) for module in modules]
        for exporter_logger in self._loggers:
            exporter_logger.addFilter(self._filter)

    def export(self, spans: Any) -> SpanExportResult:
        self._filter.active.value = True
        try:
            result = self._exporter.export(spans)
        except Exception:
            logger.debug("Trace export failed", exc_info=True)
            result = SpanExportResult.FAILURE
        finally:
            self._filter.active.value = False
        if result == SpanExportResult.FAILURE and not self._failed:
            logger.warning(
                "Trace export failed; check the OTLP endpoint and protocol. Function results are unaffected."
            )
        self._failed = result == SpanExportResult.FAILURE
        return result

    def shutdown(self) -> None:
        try:
            self._exporter.shutdown()
        finally:
            for exporter_logger in self._loggers:
                exporter_logger.removeFilter(self._filter)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._exporter.force_flush(timeout_millis)


class _ExportLogFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self.active = threading.local()

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(self.active, "value", False) and record.levelno >= logging.WARNING:
            logger.debug("%s", record.getMessage(), exc_info=record.exc_info)
            return False
        return True
