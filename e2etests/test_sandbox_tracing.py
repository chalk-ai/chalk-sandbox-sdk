"""End-to-end regression test: span export from inside a sandbox reaches the collector.

A sandbox's ``OTEL_EXPORTER_OTLP_ENDPOINT`` points the Python OTel exporter at
``unix:///run/chalk-otel/otlp.sock`` -- a Unix domain socket, not a ``host:port``.
grpc-python derives the HTTP/2 ``:authority`` header from that socket path when
no real host is given, and the collector (fronted by an Envoy ``tcp_proxy``
that can't rewrite HTTP/2 framing) rejects that bogus authority with
``RST_STREAM``, surfaced to the caller as ``StatusCode.INTERNAL``. Every span a
sandboxed process tried to export over gRPC failed silently as a result.

``chalkcompute._telemetry_export.create_span_exporter`` now pins
``grpc.default_authority`` to a real hostname whenever the resolved endpoint is
a ``unix:`` socket. This test proves both halves against a live sandbox and
the real collector:

1. The bug is real: a raw, unpatched ``OTLPSpanExporter`` (no authority
   override) fails to export over the sandbox's unix-socket endpoint.
2. The shipped fix resolves it: ``create_span_exporter`` -- the actual
   production function, not a reimplementation -- succeeds under the same
   conditions.

``_telemetry_export.py`` has no dependency on chalkcompute's native extension
(stdlib + ``opentelemetry-sdk`` only), so its current source is pushed into
the sandbox via ``sandbox.fs.write_bytes`` and imported directly, rather than
installing the full ``chalkcompute`` package (which would need a Linux build
of the native extension and access to private deps) or hand-copying its
logic, which could silently drift from what actually ships.
"""

from pathlib import Path

import pytest

from chalkcompute import Image, SandboxClient
from chalkcompute._connect import grpc_target_from_url
from e2etests.conftest import CHALK_API_SERVER, CHALK_ENVIRONMENT_ID, needs_staging

TRACING_IMAGE = Image.debian_slim("3.11").pip_install(
    ["opentelemetry-sdk", "opentelemetry-exporter-otlp-proto-grpc"]
)

# Read at test time so this always exercises the current shipped
# implementation, never a hand-copy that can drift from it.
_TELEMETRY_EXPORT_SRC = (
    Path(__file__).parent.parent / "chalkcompute" / "_telemetry_export.py"
).read_text()

_HARNESS = """
import os
os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "grpc"
# OTEL_EXPORTER_OTLP_ENDPOINT is already set by the sandbox runtime to the
# unix-socket collector endpoint.

def _try_export(build_exporter, label):
    # BatchSpanProcessor.force_flush() reports whether the flush completed
    # within its timeout, not whether the underlying export() call actually
    # succeeded -- it returns True even when every export attempt failed. So
    # the span is captured with a SimpleSpanProcessor and handed to
    # exporter.export() directly, whose SpanExportResult is authoritative.
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter

    captured = []

    class _Capture(SpanExporter):
        def export(self, spans):
            captured.extend(spans)
            from opentelemetry.sdk.trace.export import SpanExportResult
            return SpanExportResult.SUCCESS

        def shutdown(self):
            pass

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_Capture()))
    tracer = provider.get_tracer("repro")
    with tracer.start_as_current_span("sandbox-repro-span-" + label):
        pass
    provider.shutdown()

    exporter = build_exporter()
    result = exporter.export(captured)
    exporter.shutdown()
    print(label.upper() + "_EXPORT_RESULT", result.name)

# 1. Baseline: the raw exporter with no authority override. This is the bug --
#    expected to fail against the real collector.
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
_try_export(lambda: OTLPSpanExporter(), "baseline")

# 2. The actual shipped fix.
import sys
sys.path.insert(0, "/tmp")
import _telemetry_export
_try_export(_telemetry_export.create_span_exporter, "patched")
"""


def _is_local_api() -> bool:
    return CHALK_API_SERVER.startswith(("http://localhost", "http://127.0.0.1"))


@pytest.fixture(scope="module")
def grpc_client(access_token: str) -> SandboxClient:
    """Create a gRPC SandboxClient pointed at the configured API server."""
    target, use_tls = grpc_target_from_url(CHALK_API_SERVER)
    metadata = [
        ("authorization", f"Bearer {access_token}"),
        ("x-chalk-env-id", CHALK_ENVIRONMENT_ID),
    ]
    client = SandboxClient(target, use_tls=use_tls, metadata=metadata)
    yield client
    client.close()


@needs_staging
@pytest.mark.xfail(
    not _is_local_api(),
    reason="read timeout due to tcp interceptor issues",
)
def test_span_export_from_sandbox_reproduces_and_fixes_authority_bug(
    grpc_client: SandboxClient,
) -> None:
    sandbox = grpc_client.create(
        TRACING_IMAGE, name="test-tracing-e2e", tracing=True, timeout=600
    )
    try:
        sandbox.fs.write_bytes(
            "/tmp/_telemetry_export.py", _TELEMETRY_EXPORT_SRC.encode()
        )

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        exit_code: int | None = None
        for event in sandbox.exec_stream("python3", "-c", _HARNESS, timeout_secs=120):
            if event.stdout:
                stdout_parts.append(event.stdout)
            if event.stderr:
                stderr_parts.append(event.stderr)
            if event.is_exited:
                exit_code = event.exit_code
            if event.is_error:
                pytest.fail(f"Exec error: {event.error_code}: {event.error_message}")

        stdout = "".join(stdout_parts)
        stderr = "".join(stderr_parts)
        print(f"stdout:\n{stdout}\nstderr:\n{stderr}")

        assert exit_code == 0, f"harness script exited with code {exit_code}"

        # 1. Prove the bug is real: the unpatched exporter must fail against
        #    the real collector. If this starts passing, the RST_STREAM
        #    behavior this test guards may no longer reproduce, and the
        #    assertions here need to be revisited rather than loosened.
        assert "BASELINE_EXPORT_RESULT FAILURE" in stdout, (
            "expected the raw OTLPSpanExporter (no authority override) to "
            f"fail against the real collector. stdout:\n{stdout}"
        )
        assert "Failed to export traces" in stderr

        # 2. Prove the shipped fix actually resolves it.
        assert "PATCHED_EXPORT_RESULT SUCCESS" in stdout, (
            f"expected create_span_exporter's fix to succeed. stdout:\n{stdout}"
        )
    finally:
        sandbox.terminate()
