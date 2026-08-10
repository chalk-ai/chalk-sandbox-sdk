"""Tests for the process-wide shared gRPC channel registry."""

from __future__ import annotations

import json
from types import SimpleNamespace

import grpc
import pytest

import chalkcompute._channel as channel_mod
from chalkcompute._channel import default_channel_options, get_channel


@pytest.fixture(autouse=True)
def _clean_registry():
    channel_mod.close_all()
    yield
    channel_mod.close_all()


def test_same_key_returns_same_channel() -> None:
    a = get_channel("localhost:50051", False)
    b = get_channel("localhost:50051", False)
    assert a is b


def test_distinct_targets_get_distinct_channels() -> None:
    a = get_channel("localhost:50051", False)
    b = get_channel("localhost:50052", False)
    assert a is not b


def test_tls_flag_is_part_of_the_key() -> None:
    a = get_channel("localhost:50051", False)
    b = get_channel("localhost:50051", True)
    assert a is not b


def test_close_all_empties_registry() -> None:
    a = get_channel("localhost:50051", False)
    channel_mod.close_all()
    b = get_channel("localhost:50051", False)
    assert a is not b


def test_default_options_include_keepalive_and_retries() -> None:
    options = dict(default_channel_options())
    assert options["grpc.keepalive_time_ms"] == 60_000
    assert options["grpc.keepalive_permit_without_calls"] == 1
    assert options["grpc.max_receive_message_length"] == -1
    service_config = json.loads(options["grpc.service_config"])
    retry = service_config["methodConfig"][0]["retryPolicy"]
    assert retry["retryableStatusCodes"] == ["UNAVAILABLE"]
    assert retry["maxAttempts"] == 5


def _intercept_with_recording_tracer(monkeypatch, status_code: grpc.StatusCode):
    """Run the tracing interceptor around a fake RPC and return its span."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(channel_mod, "get_tracer", lambda: provider.get_tracer("test"))

    fake_call = SimpleNamespace(code=lambda: status_code)
    details = SimpleNamespace(method=b"/chalk.server.v1.ContainerService/GetContainer")
    channel_mod._TracingInterceptor().intercept_unary_unary(
        lambda details, request: fake_call, details, request=None
    )

    (span,) = exporter.get_finished_spans()
    return span


def test_rpc_span_contract(monkeypatch) -> None:
    from opentelemetry.trace import StatusCode

    span = _intercept_with_recording_tracer(monkeypatch, grpc.StatusCode.OK)
    assert span.name == "chalkcompute.rpc"
    assert span.attributes["rpc.system.name"] == "grpc"
    assert (
        span.attributes["rpc.method"] == "chalk.server.v1.ContainerService/GetContainer"
    )
    assert span.attributes["rpc.response.status_code"] == "OK"
    assert span.status.status_code is StatusCode.UNSET


def test_rpc_span_marks_failed_calls_as_errors(monkeypatch) -> None:
    from opentelemetry.trace import StatusCode

    span = _intercept_with_recording_tracer(monkeypatch, grpc.StatusCode.UNAVAILABLE)
    assert span.attributes["rpc.response.status_code"] == "UNAVAILABLE"
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "UNAVAILABLE"


def test_dead_target_construction_is_safe() -> None:
    # gRPC dials lazily; building a channel to an unreachable target must not
    # raise or block. (Unit tests rely on this everywhere.)
    channel = get_channel("localhost:1", False)
    assert channel is not None
