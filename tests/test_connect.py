from typing import Any

from chalkcompute._connect import (
    ConnectClient,
    grpc_engine_target_from_url,
    grpc_target_from_url,
)
from chalkcompute._gen.chalk.server.v1 import auth_connect, auth_pb2


def _patch_auth_client(
    monkeypatch: Any,
    *,
    get_token_response: auth_pb2.GetTokenResponse | None = None,
    internal_token_response: auth_pb2.GetInternalWorkingTokenResponse | None = None,
) -> dict[str, Any]:
    calls: dict[str, Any] = {}

    class FakeAuthServiceClientSync:
        def __init__(self, base_url: str, **kwargs: Any) -> None:
            calls["base_url"] = base_url
            calls["init_kwargs"] = kwargs

        def get_token(
            self,
            request: auth_pb2.GetTokenRequest,
            *,
            headers: dict[str, str] | None = None,
            timeout_ms: int | None = None,
        ) -> auth_pb2.GetTokenResponse:
            calls["get_token_request"] = request
            calls["get_token_headers"] = headers
            calls["get_token_timeout_ms"] = timeout_ms
            assert get_token_response is not None
            return get_token_response

        def get_internal_working_token(
            self,
            request: auth_pb2.GetInternalWorkingTokenRequest,
            *,
            headers: dict[str, str] | None = None,
            timeout_ms: int | None = None,
        ) -> auth_pb2.GetInternalWorkingTokenResponse:
            calls["internal_token_request"] = request
            calls["internal_token_headers"] = headers
            calls["internal_token_timeout_ms"] = timeout_ms
            assert internal_token_response is not None
            return internal_token_response

        def close(self) -> None:
            calls["closed"] = True

    monkeypatch.setattr(
        auth_connect, "AuthServiceClientSync", FakeAuthServiceClientSync
    )
    return calls


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
        token_response = auth_pb2.GetTokenResponse(
            access_token="token",
            primary_environment="env-1",
        )
        token_response.grpc_engines["env-1"] = "engine.staging.chalk.dev:443"
        token_response.environment_id_to_name["env-1"] = "Staging"
        _patch_auth_client(monkeypatch, get_token_response=token_response)

        target, use_tls = ConnectClient().grpc_engine_target()

        assert target == "engine.staging.chalk.dev:443"
        assert use_tls is True

    def test_resolves_environment_name_before_engine_lookup(self, monkeypatch):
        monkeypatch.setenv("CHALK_API_SERVER", "https://api.staging.chalk.dev")
        monkeypatch.setenv("CHALK_CLIENT_ID", "client-id")
        monkeypatch.setenv("CHALK_CLIENT_SECRET", "client-secret")
        monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "staging")
        token_response = auth_pb2.GetTokenResponse(
            access_token="token",
            primary_environment="env-1",
        )
        token_response.grpc_engines["env-1"] = "localhost:50051"
        token_response.environment_id_to_name["env-1"] = "Staging"
        _patch_auth_client(monkeypatch, get_token_response=token_response)

        target, use_tls = ConnectClient().grpc_engine_target()

        assert target == "localhost:50051"
        assert use_tls is False


class TestConnectClientServiceAuth:
    def test_uses_exchange_token_without_client_id(self, monkeypatch):
        monkeypatch.setenv("CHALK_API_SERVER", "https://api.chalk.test")
        monkeypatch.setenv("CHALK_REMOTE_AUTH_EXCHANGE_TOKEN", "exchange-token")
        monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "env-1")
        calls = _patch_auth_client(
            monkeypatch,
            internal_token_response=auth_pb2.GetInternalWorkingTokenResponse(
                access_token="working-token"
            ),
        )

        client = ConnectClient()
        client._ensure_auth()

        assert calls["base_url"] == "https://api.chalk.test"
        assert list(calls["internal_token_request"].requested_permissions) == [2, 3, 11]
        assert (
            calls["internal_token_headers"]["Authorization"] == "Bearer exchange-token"
        )
        assert client._headers is not None
        assert client._headers["Authorization"] == "Bearer working-token"
        assert client._headers["X-Chalk-Env-Id"] == "env-1"
