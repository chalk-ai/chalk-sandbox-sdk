import contextlib
from typing import Any

from chalkcompute._connect import ConnectClient, grpc_engine_target_from_url, grpc_target_from_url


class _TokenResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._payload


class _RPCResponse:
    status_code = 200


class _Span:
    def set_attribute(self, *_args: object) -> None:
        pass

    def set_status(self, *_args: object) -> None:
        pass


class _Tracer:
    @contextlib.contextmanager
    def start_as_current_span(self, _name: str):
        yield _Span()


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
    def _client(self, monkeypatch) -> ConnectClient:
        client = ConnectClient()
        client._api_server = "https://api.chalk.test"
        client._headers = {"Authorization": "Bearer token"}
        monkeypatch.setattr(client, "_ensure_auth", lambda: None)
        return client

    def test_rpc_does_not_propagate_new_root_trace(self, monkeypatch):
        posted: dict[str, Any] = {}

        def fake_post(*args, **kwargs):
            posted["kwargs"] = kwargs
            return _RPCResponse()

        def fail_current_trace_metadata():
            raise AssertionError("trace metadata should not be injected without upstream trace")

        monkeypatch.setattr("httpx.post", fake_post)
        monkeypatch.setattr("chalkcompute._tracing.get_tracer", lambda: _Tracer())
        monkeypatch.setattr("chalkcompute._tracing.has_current_trace_context", lambda: False)
        monkeypatch.setattr("chalkcompute._tracing.current_trace_metadata", fail_current_trace_metadata)

        self._client(monkeypatch).rpc("svc", "Method", {})

        assert posted["kwargs"]["headers"] == {"Authorization": "Bearer token"}

    def test_rpc_propagates_existing_trace_context(self, monkeypatch):
        monkeypatch.setenv("CHALK_REMOTE_FUNCTION_TRACING", "1")
        posted: dict[str, Any] = {}

        def fake_post(*args, **kwargs):
            posted["kwargs"] = kwargs
            return _RPCResponse()

        monkeypatch.setattr("httpx.post", fake_post)
        monkeypatch.setattr("chalkcompute._tracing.get_tracer", lambda: _Tracer())
        monkeypatch.setattr("chalkcompute._tracing.has_current_trace_context", lambda: True)
        monkeypatch.setattr(
            "chalkcompute._tracing.current_trace_metadata",
            lambda: [("traceparent", "trace-id")],
        )

        client = self._client(monkeypatch)
        client.rpc("svc", "Method", {})

        assert posted["kwargs"]["headers"]["Authorization"] == "Bearer token"
        assert posted["kwargs"]["headers"]["traceparent"] == "trace-id"
        assert "traceparent" not in client._headers


class TestConnectClientServiceAuth:
    def test_uses_exchange_token_without_client_id(self, monkeypatch):
        monkeypatch.setenv("CHALK_REMOTE_AUTH_ENDPOINT", "https://api.chalk.test")
        monkeypatch.setenv("CHALK_REMOTE_AUTH_EXCHANGE_TOKEN", "exchange-token")
        monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "env-1")
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
