import logging

import pytest
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from chalkcompute._telemetry_export import _DiagnosticExporter, create_span_exporter


def test_export_outage_is_reported_once_without_muting_other_exporters(caplog):
    class FailingExporter(SpanExporter):
        def export(self, spans):
            logging.getLogger(__name__).warning("internal retry diagnostic")
            return SpanExportResult.FAILURE

    caplog.set_level(logging.WARNING)
    exporter = _DiagnosticExporter(FailingExporter())
    try:
        exporter.export([])
        exporter.export([])
        assert caplog.text.count("Trace export failed;") == 1
        assert "internal retry diagnostic" not in caplog.text
        FailingExporter().export([])
        assert "internal retry diagnostic" in caplog.text
    finally:
        exporter.shutdown()


def test_signal_protocol_takes_precedence(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "grpc")
    exporter = create_span_exporter()
    try:
        assert ".grpc." in type(exporter._exporter).__module__
    finally:
        exporter.shutdown()


def test_default_protocol_stays_http(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_PROTOCOL", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", raising=False)
    exporter = create_span_exporter()
    try:
        assert ".http." in type(exporter._exporter).__module__
    finally:
        exporter.shutdown()


@pytest.mark.parametrize("protocol", ["grpc", "http/protobuf"])
def test_exporter_implementation_logs_are_scoped_and_available_at_debug(
    monkeypatch, caplog, protocol
):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", protocol)
    exporter = create_span_exporter()
    module = (
        "opentelemetry.exporter.otlp.proto.grpc.exporter"
        if protocol == "grpc"
        else "opentelemetry.exporter.otlp.proto.http.trace_exporter"
    )
    implementation_logger = logging.getLogger(module)
    results = iter(
        [SpanExportResult.FAILURE, SpanExportResult.SUCCESS, SpanExportResult.FAILURE]
    )

    def export(spans):
        implementation_logger.warning("retry detail for %s", protocol)
        return next(results)

    monkeypatch.setattr(exporter._exporter, "export", export)
    caplog.set_level(logging.DEBUG, logger="chalkcompute._telemetry_export")
    try:
        for _ in range(3):
            exporter.export([])
        assert caplog.text.count("Trace export failed;") == 2
        diagnostics = [r for r in caplog.records if "retry detail" in r.message]
        assert diagnostics
        assert all(r.levelno == logging.DEBUG for r in diagnostics)
    finally:
        exporter.shutdown()
