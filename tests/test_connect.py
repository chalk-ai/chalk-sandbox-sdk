import asyncio
import contextlib
from typing import Any

import pytest

from chalkcompute import SandboxClient, _tracing
from chalkcompute._connect import (
    ConnectClient,
    grpc_engine_target_from_url,
    grpc_target_from_url,
)
from chalkcompute._remote_call_client import ServiceAuthContext


class _TokenResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._payload


class _RPCResponse:
    status_code = 200


class _SpanContext:
    is_valid = True


class _Span:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}
        self.statuses: list[tuple[object, ...]] = []
        self.recorded_exceptions: list[tuple[object, ...]] = []

    def set_attribute(self, *_args: object) -> None:
        if len(_args) == 2:
            self.attributes[str(_args[0])] = _args[1]

    def set_status(self, *_args: object) -> None:
        self.statuses.append(_args)

    def record_exception(self, *_args: object) -> None:
        self.recorded_exceptions.append(_args)

    def get_span_context(self) -> _SpanContext:
        return _SpanContext()


class _Tracer:
    def __init__(self) -> None:
        self.span_names: list[str] = []
        self.spans: list[_Span] = []
        self.active_span_names: list[str] = []

    @contextlib.contextmanager
    def start_as_current_span(self, name: str, *_args: object, **_kwargs: object):
        self.span_names.append(name)
        span = _Span()
        self.spans.append(span)
        self.active_span_names.append(name)
        try:
            yield span
        finally:
            self.active_span_names.pop()


class _OtelContext:
    @staticmethod
    def get_current() -> object:
        return object()

    @staticmethod
    def attach(_context: object) -> object:
        return object()

    @staticmethod
    def detach(_token: object) -> None:
        pass


class _TraceApi:
    class SpanKind:
        CLIENT = object()


class TestGrpcTargetFromUrl:
    def test_https_defaults_to_443(self):
        target, use_tls = grpc_target_from_url("https://api.chalk.ai")

        assert target == "api.chalk.ai:443"
        assert use_tls is True

    def test_https_preserves_explicit_port(self):
        target, use_tls = grpc_target_from_url("https://api.chalk.ai:7443/path")

        assert target == "api.chalk.ai:7443"
        assert use_tls is True

    def test_http_uses_insecure_fallback_port(self):
        target, use_tls = grpc_target_from_url(
            "http://function.example.com/run",
            insecure_port=8080,
        )

        assert target == "function.example.com:8080"
        assert use_tls is False

    def test_http_preserves_explicit_port(self):
        target, use_tls = grpc_target_from_url(
            "http://function.example.com:9000/run",
            insecure_port=8080,
        )

        assert target == "function.example.com:9000"
        assert use_tls is False


class TestGrpcEngineTargetFromUrl:
    def test_unschemed_external_engine_defaults_to_tls(self):
        target, use_tls = grpc_engine_target_from_url("engine.staging.chalk.dev:443")

        assert target == "engine.staging.chalk.dev:443"
        assert use_tls is True

    def test_localhost_engine_defaults_to_insecure(self):
        target, use_tls = grpc_engine_target_from_url("localhost:50051")

        assert target == "localhost:50051"
        assert use_tls is False


class TestConnectClientGrpcEngineTarget:
    def test_uses_grpc_engine_from_token_response(self, monkeypatch):
        monkeypatch.setenv("CHALK_API_SERVER", "https://api.staging.chalk.dev")
        monkeypatch.setenv("CHALK_CLIENT_ID", "client-id")
        monkeypatch.setenv("CHALK_CLIENT_SECRET", "client-secret")
        monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "env-1")
        monkeypatch.setattr(
            "httpx.post",
            lambda *args, **kwargs: _TokenResponse(
                {
                    "access_token": "token",
                    "primary_environment": "env-1",
                    "grpc_engines": {"env-1": "engine.staging.chalk.dev:443"},
                    "environment_id_to_name": {"env-1": "Staging"},
                }
            ),
        )

        target, use_tls = ConnectClient().grpc_engine_target()

        assert target == "engine.staging.chalk.dev:443"
        assert use_tls is True

    def test_resolves_environment_name_before_engine_lookup(self, monkeypatch):
        monkeypatch.setenv("CHALK_API_SERVER", "https://api.staging.chalk.dev")
        monkeypatch.setenv("CHALK_CLIENT_ID", "client-id")
        monkeypatch.setenv("CHALK_CLIENT_SECRET", "client-secret")
        monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "staging")
        monkeypatch.setattr(
            "httpx.post",
            lambda *args, **kwargs: _TokenResponse(
                {
                    "access_token": "token",
                    "primary_environment": "env-1",
                    "grpc_engines": {"env-1": "localhost:50051"},
                    "environment_id_to_name": {"env-1": "Staging"},
                }
            ),
        )

        target, use_tls = ConnectClient().grpc_engine_target()

        assert target == "localhost:50051"
        assert use_tls is False


class TestConnectClientTracing:
    def test_internal_configure_tracing_installs_parent_based_sampler(
        self, monkeypatch
    ):
        rates: list[float] = []
        monkeypatch.setattr(_tracing, "_initialized", False)
        monkeypatch.setattr(_tracing, "_provider", None)
        monkeypatch.setattr(_tracing, "_user_tracing_configured", False)
        monkeypatch.setattr(
            _tracing,
            "_install_tracer_provider",
            lambda sample_rate: rates.append(sample_rate),
        )

        _tracing._configure_tracing(sample_rate=0.1)

        assert rates == [0.1]
        assert _tracing._user_tracing_configured is True

    def test_trace_defaults_to_sample_rate_one_when_unconfigured(self, monkeypatch):
        rates: list[float] = []
        tracer = _Tracer()
        monkeypatch.setattr(_tracing, "_initialized", False)
        monkeypatch.setattr(_tracing, "_provider", None)
        monkeypatch.setattr(_tracing, "_user_tracing_configured", False)
        monkeypatch.setattr(
            _tracing,
            "_install_tracer_provider",
            lambda sample_rate: rates.append(sample_rate),
        )
        monkeypatch.setattr(_tracing, "get_tracer", lambda: tracer)

        with _tracing.trace("workflow"):
            pass

        assert rates == [1.0]
        assert tracer.span_names == ["workflow"]

    def test_trace_leaves_exception_recording_to_otel_context_manager(
        self, monkeypatch
    ):
        tracer = _Tracer()
        monkeypatch.setattr(_tracing, "_initialized", False)
        monkeypatch.setattr(_tracing, "_provider", None)
        monkeypatch.setattr(_tracing, "_user_tracing_configured", False)
        monkeypatch.setattr(_tracing, "_install_tracer_provider", lambda _rate: None)
        monkeypatch.setattr(_tracing, "get_tracer", lambda: tracer)

        with pytest.raises(RuntimeError, match="boom"):
            with _tracing.trace("workflow"):
                raise RuntimeError("boom")

        assert tracer.spans[0].recorded_exceptions == []
        assert tracer.spans[0].statuses == []

    def test_trace_noops_when_remote_tracing_is_off(self, monkeypatch):
        monkeypatch.setenv("CHALK_REMOTE_FUNCTION_TRACE_POLICY", "always_off")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector")
        monkeypatch.setattr(
            _tracing,
            "_install_tracer_provider",
            lambda _rate: (_ for _ in ()).throw(
                AssertionError("off tracing should not configure a provider")
            ),
        )
        monkeypatch.setattr(
            _tracing,
            "get_tracer",
            lambda: (_ for _ in ()).throw(
                AssertionError("off tracing should not request a tracer")
            ),
        )

        with _tracing.trace("workflow") as span:
            assert span is None

        assert _tracing.current_trace_metadata() == []

    def test_traced_decorator_uses_explicit_name(self, monkeypatch):
        tracer = _Tracer()
        monkeypatch.setattr(_tracing, "_initialized", False)
        monkeypatch.setattr(_tracing, "_provider", None)
        monkeypatch.setattr(_tracing, "_user_tracing_configured", False)
        monkeypatch.setattr(_tracing, "_install_tracer_provider", lambda _rate: None)
        monkeypatch.setattr(_tracing, "get_tracer", lambda: tracer)

        @_tracing.traced("workflow")
        def run_workflow() -> int:
            return 42

        assert run_workflow() == 42
        assert tracer.span_names == ["workflow"]

    def test_bare_traced_decorator_defaults_to_function_name(self, monkeypatch):
        tracer = _Tracer()
        monkeypatch.setattr(_tracing, "_initialized", False)
        monkeypatch.setattr(_tracing, "_provider", None)
        monkeypatch.setattr(_tracing, "_user_tracing_configured", False)
        monkeypatch.setattr(_tracing, "_install_tracer_provider", lambda _rate: None)
        monkeypatch.setattr(_tracing, "get_tracer", lambda: tracer)

        @_tracing.traced
        def run_workflow() -> int:
            return 42

        assert run_workflow() == 42
        assert tracer.span_names == ["run_workflow"]

    def test_async_traced_decorator_keeps_span_open_while_awaiting(self, monkeypatch):
        tracer = _Tracer()
        monkeypatch.setattr(_tracing, "_initialized", False)
        monkeypatch.setattr(_tracing, "_provider", None)
        monkeypatch.setattr(_tracing, "_user_tracing_configured", False)
        monkeypatch.setattr(_tracing, "_install_tracer_provider", lambda _rate: None)
        monkeypatch.setattr(_tracing, "get_tracer", lambda: tracer)

        @_tracing.traced("workflow")
        async def run_workflow() -> list[str]:
            await asyncio.sleep(0)
            return list(tracer.active_span_names)

        assert asyncio.run(run_workflow()) == ["workflow"]
        assert tracer.span_names == ["workflow"]

    def test_remote_function_call_span_noops_without_active_trace(self, monkeypatch):
        monkeypatch.setattr(
            "chalkcompute._tracing.has_current_trace_context",
            lambda: False,
        )
        monkeypatch.setattr(
            "chalkcompute._tracing.get_tracer",
            lambda: (_ for _ in ()).throw(
                AssertionError("remote function call should not start a root span")
            ),
        )

        with _tracing.remote_function_call_span("embed", "direct") as span:
            assert span is None

    def test_remote_call_span_leaves_exception_recording_to_otel_context_manager(
        self, monkeypatch
    ):
        tracer = _Tracer()
        monkeypatch.setattr(_tracing, "has_current_trace_context", lambda: True)
        monkeypatch.setattr(_tracing, "get_tracer", lambda: tracer)
        monkeypatch.setattr(_tracing, "_get_context_api", lambda: _OtelContext)
        monkeypatch.setattr(_tracing, "_get_trace_api", lambda: _TraceApi)

        with pytest.raises(RuntimeError, match="boom"):
            with _tracing.remote_function_call_span("embed", "direct"):
                raise RuntimeError("boom")

        assert tracer.spans[0].recorded_exceptions == []
        assert tracer.spans[0].statuses == []

    def _client(self, monkeypatch) -> ConnectClient:
        client = ConnectClient()
        client._api_server = "https://api.chalk.test"
        client._headers = {"Authorization": "Bearer token"}
        monkeypatch.setattr(client, "_ensure_auth", lambda: None)
        return client

    def test_rpc_does_not_inject_trace_metadata(self, monkeypatch):
        posted: dict[str, Any] = {}

        def fake_post(*args, **kwargs):
            posted["kwargs"] = kwargs
            return _RPCResponse()

        def fail_current_trace_metadata():
            raise AssertionError(
                "trace metadata should not be injected without upstream trace"
            )

        monkeypatch.setattr(
            "chalkcompute._tracing.current_trace_metadata",
            fail_current_trace_metadata,
        )
        monkeypatch.setattr("httpx.post", fake_post)

        self._client(monkeypatch).rpc("svc", "Method", {})

        assert posted["kwargs"]["headers"] == {"Authorization": "Bearer token"}

    def test_rpc_merges_extra_headers_without_mutating_cached_headers(
        self, monkeypatch
    ):
        posted: dict[str, Any] = {}

        def fake_post(*args, **kwargs):
            posted["kwargs"] = kwargs
            return _RPCResponse()

        monkeypatch.setattr("httpx.post", fake_post)

        client = self._client(monkeypatch)
        client.rpc(
            "svc",
            "Method",
            {},
            extra_headers={"x-chalk-test-header": "embed"},
        )

        assert posted["kwargs"]["headers"] == {
            "Authorization": "Bearer token",
            "x-chalk-test-header": "embed",
        }
        assert client._headers == {"Authorization": "Bearer token"}


class TestConnectClientServiceAuth:
    def test_uses_exchange_token_without_client_id(self, monkeypatch):
        monkeypatch.setattr(
            "chalkcompute._connect.resolve_service_auth_context",
            lambda: ServiceAuthContext(
                auth_endpoint="https://api.chalk.test",
                exchange_token="exchange-token",
                environment_id="env-1",
                requested_permissions=(2, 3, 11),
                grpc_engine=None,
            ),
        )
        posted: dict[str, Any] = {}

        def fake_post(*args, **kwargs):
            posted["args"] = args
            posted["kwargs"] = kwargs
            return _TokenResponse({"accessToken": "working-token"})

        monkeypatch.setattr("httpx.post", fake_post)

        client = ConnectClient()
        client._ensure_auth()

        assert posted["args"] == (
            "https://api.chalk.test/chalk.server.v1.AuthService/GetInternalWorkingToken",
        )
        assert posted["kwargs"]["json"] == {"requestedPermissions": [2, 3, 11]}
        assert posted["kwargs"]["headers"]["Authorization"] == "Bearer exchange-token"
        assert client._headers is not None
        assert client._headers["Authorization"] == "Bearer working-token"
        assert client._headers["X-Chalk-Env-Id"] == "env-1"


class TestSandboxClientFromEnv:
    def test_uses_insecure_channel_for_local_http_api(self, monkeypatch):
        monkeypatch.setenv("CHALK_API_SERVER", "http://localhost:18080")
        monkeypatch.setenv("CHALK_CLIENT_ID", "client-id")
        monkeypatch.setenv("CHALK_CLIENT_SECRET", "client-secret")
        monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "env-1")
        monkeypatch.setattr(
            "httpx.post",
            lambda *args, **kwargs: _TokenResponse(
                {
                    "access_token": "token",
                    "primary_environment": "env-1",
                    "environment_id_to_name": {"env-1": "Local"},
                }
            ),
        )

        client = SandboxClient.from_env()
        try:
            assert client._target == "localhost:18080"
        finally:
            client.close()
