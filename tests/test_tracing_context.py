"""Tests for handing trace context to, and adopting it inside, a workload."""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider

from chalkcompute import _tracing

_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
_SPAN_ID = "00f067aa0ba902b7"
_TRACEPARENT = f"00-{_TRACE_ID}-{_SPAN_ID}-01"


@pytest.fixture
def fresh_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_tracing, "_initialized", False)
    monkeypatch.setattr(_tracing, "_provider", None)
    monkeypatch.setattr(_tracing, "_tracer", None)
    monkeypatch.setattr(_tracing, "_user_tracing_configured", False)
    monkeypatch.setattr(_tracing, "_ambient_context_adopted", False)
    for key in ("TRACEPARENT", "TRACESTATE", "BAGGAGE", "OTEL_TRACES_SAMPLER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("OTEL_TRACES_SAMPLER_ARG", raising=False)


def _install_sdk_provider(
    monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> TracerProvider:
    """Install the SDK provider through a fake process-wide OTel API."""
    providers: list[TracerProvider] = []

    class _TraceApi:
        @staticmethod
        def get_tracer_provider() -> object:
            return object()

        @staticmethod
        def set_tracer_provider(provider: TracerProvider) -> None:
            providers.append(provider)

    monkeypatch.setattr(_tracing, "_get_trace_api", lambda: _TraceApi)
    _tracing._install_tracer_provider(**kwargs)
    (provider,) = providers
    return provider


# --- TRACEPARENT etc. in the process environment ---------------------------


def test_ambient_traceparent_becomes_the_current_parent(
    fresh_tracing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRACEPARENT", _TRACEPARENT)
    monkeypatch.setenv("BAGGAGE", "session.id=abc")

    token = _tracing._adopt_ambient_trace_context()
    try:
        span_context = trace_api.get_current_span().get_span_context()
        assert format(span_context.trace_id, "032x") == _TRACE_ID
        assert format(span_context.span_id, "016x") == _SPAN_ID
        assert span_context.is_remote
        assert _tracing.current_session_id() == "abc"
        # And it rides on to nested calls.
        assert dict(_tracing.current_trace_metadata())["traceparent"] == _TRACEPARENT
    finally:
        assert token is not None
        context_api.detach(token)


def test_ambient_context_is_adopted_once(
    fresh_tracing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRACEPARENT", _TRACEPARENT)
    token = _tracing._adopt_ambient_trace_context()
    try:
        assert _tracing._adopt_ambient_trace_context() is None
    finally:
        assert token is not None
        context_api.detach(token)


def test_ambient_context_without_traceparent_is_ignored(fresh_tracing: None) -> None:
    assert _tracing._adopt_ambient_trace_context() is None
    assert not trace_api.get_current_span().get_span_context().is_valid


def test_malformed_ambient_traceparent_is_ignored(
    fresh_tracing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRACEPARENT", "not-a-traceparent")
    assert _tracing._adopt_ambient_trace_context() is None
    assert not trace_api.get_current_span().get_span_context().is_valid


def test_ambient_context_does_not_replace_an_in_process_parent(
    fresh_tracing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRACEPARENT", _TRACEPARENT)
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("parent") as parent:
        assert _tracing._adopt_ambient_trace_context() is None
        assert trace_api.get_current_span() is parent


def test_ensure_initialized_adopts_ambient_context_without_an_exporter(
    fresh_tracing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.setenv("TRACEPARENT", _TRACEPARENT)
    before = context_api.get_current()

    _tracing._ensure_initialized()
    try:
        assert _tracing.has_current_trace_context()
        assert _tracing.trace_headers()["traceparent"] == _TRACEPARENT
    finally:
        context_api.attach(before)


def test_trace_env_uses_environment_variable_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _tracing,
        "current_trace_metadata",
        lambda: [("traceparent", _TRACEPARENT), ("baggage", "k=v")],
    )
    assert _tracing.trace_env() == {"TRACEPARENT": _TRACEPARENT, "BAGGAGE": "k=v"}


# --- OTEL_TRACES_SAMPLER inside a workload ---------------------------------


def _root_sampler_description(provider: TracerProvider) -> str:
    return provider.sampler.get_description()


def test_sdk_provider_defaults_to_full_root_sampling(
    fresh_tracing: None, monkeypatch
) -> None:
    assert _tracing._default_sample_rate() == 1.0
    provider = _install_sdk_provider(
        monkeypatch, sample_rate=_tracing._default_sample_rate()
    )
    assert "TraceIdRatioBased{1.0}" in _root_sampler_description(provider)


def test_sdk_provider_honours_otel_sampler_env(
    fresh_tracing: None, monkeypatch
) -> None:
    """``Sandbox(tracing=TracingPolicy(0.25))`` sets these in the sandbox."""
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "parentbased_traceidratio")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "0.25")
    assert _tracing._default_sample_rate() is None
    provider = _install_sdk_provider(
        monkeypatch, sample_rate=_tracing._default_sample_rate()
    )
    assert "TraceIdRatioBased{0.25}" in _root_sampler_description(provider)


def test_explicit_sample_rate_overrides_otel_sampler_env(
    fresh_tracing: None, monkeypatch
) -> None:
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_off")
    provider = _install_sdk_provider(monkeypatch, sample_rate=0.5)
    assert "TraceIdRatioBased{0.5}" in _root_sampler_description(provider)


def test_intent_default_defers_to_otel_sampler_env(
    fresh_tracing: None, monkeypatch
) -> None:
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_off")
    providers: list[TracerProvider] = []

    class _TraceApi:
        @staticmethod
        def get_tracer_provider() -> object:
            return object()

        @staticmethod
        def set_tracer_provider(provider: TracerProvider) -> None:
            providers.append(provider)

    monkeypatch.setattr(_tracing, "_get_trace_api", lambda: _TraceApi)
    _tracing._request_tracing_intent()
    (provider,) = providers
    assert provider.sampler.get_description() == "AlwaysOffSampler"
    assert _tracing._user_tracing_configured
