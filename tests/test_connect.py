import base64
import contextlib
import json
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

import chalkcompute._connect as _connect
import chalkcompute._auth_config as _auth_config
import chalkcompute._web_identity as _web_identity
from chalkcompute import SandboxClient, _tracing
from chalkcompute._connect import (
    ConnectError,
    ConnectClient,
    chalk_client_from_env,
    grpc_engine_target_from_url,
    grpc_target_from_url,
)
from chalkcompute._gen.chalk.server.v1 import auth_connect, auth_pb
from chalkcompute._http import SYNC_HTTP_CLIENT
from chalkcompute._remote_call_client import ServiceAuthContext


def _install_auth_client(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    captured: dict[str, Any],
) -> None:
    class _AuthClient:
        def __init__(self, address: str, **kwargs: Any) -> None:
            captured["init_count"] = captured.get("init_count", 0) + 1
            captured["address"] = address
            captured["init_kwargs"] = kwargs

        def get_internal_working_token(
            self, request: Any, **kwargs: Any
        ) -> auth_pb.GetInternalWorkingTokenResponse:
            captured["request"] = request
            captured["call_kwargs"] = kwargs
            return auth_pb.GetInternalWorkingTokenResponse.from_json(
                json.dumps(payload)
            )

        def close(self) -> None:
            captured["close_count"] = captured.get("close_count", 0) + 1
            captured["closed"] = True

    monkeypatch.setattr(
        "chalkcompute._connect.auth_connect.AuthServiceClientSync", _AuthClient
    )


def _install_token_client(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    captured: dict[str, Any] | None = None,
) -> None:
    captured = {} if captured is None else captured

    class _AuthClient:
        def __init__(self, address: str, **kwargs: Any) -> None:
            captured["init_count"] = captured.get("init_count", 0) + 1
            captured["address"] = address
            captured["init_kwargs"] = kwargs

        def get_token(self, request: Any, **kwargs: Any) -> auth_pb.GetTokenResponse:
            captured["request"] = request
            captured["call_kwargs"] = kwargs
            return auth_pb.GetTokenResponse.from_json(json.dumps(payload))

        def close(self) -> None:
            captured["close_count"] = captured.get("close_count", 0) + 1
            captured["closed"] = True

    monkeypatch.setattr(
        "chalkcompute._connect.auth_connect.AuthServiceClientSync", _AuthClient
    )


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

    @contextlib.contextmanager
    def start_as_current_span(self, name: str, *_args: object, **_kwargs: object):
        self.span_names.append(name)
        span = _Span()
        self.spans.append(span)
        yield span


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


def _identity_jwt(
    environment_id: str,
    *,
    subject: str = "workload",
    expires_at: object | None = None,
) -> str:
    def encode(value: object) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    claims: dict[str, object] = {
        "environment_id": environment_id,
        "sub": subject,
    }
    if expires_at is not None:
        claims["exp"] = expires_at
    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(claims)}.signature"


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
    @pytest.fixture(autouse=True)
    def disable_service_auth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "chalkcompute._connect.resolve_service_auth_context", lambda: None
        )

    def setup_method(self) -> None:
        _auth_config._load_global_config.cache_clear()

    def teardown_method(self) -> None:
        _auth_config._load_global_config.cache_clear()

    def test_uses_grpc_engine_from_token_response(self, monkeypatch):
        monkeypatch.setenv("CHALK_API_SERVER", "https://api.staging.chalk.dev")
        monkeypatch.setenv("CHALK_CLIENT_ID", "client-id")
        monkeypatch.setenv("CHALK_CLIENT_SECRET", "client-secret")
        monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "env-1")
        _install_token_client(
            monkeypatch,
            {
                "accessToken": "token",
                "primaryEnvironment": "env-1",
                "grpcEngines": {"env-1": "engine.staging.chalk.dev:443"},
                "environmentIdToName": {"env-1": "Staging"},
            },
        )

        target, use_tls = ConnectClient().grpc_engine_target()

        assert target == "engine.staging.chalk.dev:443"
        assert use_tls is True

    def test_resolves_environment_name_before_engine_lookup(self, monkeypatch):
        monkeypatch.setenv("CHALK_API_SERVER", "https://api.staging.chalk.dev")
        monkeypatch.setenv("CHALK_CLIENT_ID", "client-id")
        monkeypatch.setenv("CHALK_CLIENT_SECRET", "client-secret")
        monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "staging")
        _install_token_client(
            monkeypatch,
            {
                "accessToken": "token",
                "primaryEnvironment": "env-1",
                "grpcEngines": {"env-1": "localhost:50051"},
                "environmentIdToName": {"env-1": "Staging"},
            },
        )

        target, use_tls = ConnectClient().grpc_engine_target()

        assert target == "localhost:50051"
        assert use_tls is False

    def test_loads_default_token_from_chalk_login_config(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CHALK_API_SERVER", raising=False)
        monkeypatch.delenv("CHALK_CLIENT_ID", raising=False)
        monkeypatch.delenv("CHALK_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("CHALK_ENVIRONMENT", raising=False)
        monkeypatch.delenv("CHALK_ENVIRONMENT_ID", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        (tmp_path / ".chalk.yml").write_text(
            """
tokens:
  default:
    clientId: config-client
    clientSecret: config-secret
    apiServer: https://api.config.chalk.ai
    activeEnvironment: Staging
""",
        )
        posted: dict[str, Any] = {}

        _install_token_client(
            monkeypatch,
            {
                "accessToken": "token",
                "primaryEnvironment": "env-primary",
                "grpcEngines": {"env-1": "engine.staging.chalk.dev:443"},
                "environmentIdToName": {"env-1": "Staging"},
            },
            posted,
        )

        client = ConnectClient()
        client._ensure_auth()

        assert posted["address"] == "https://api.config.chalk.ai"
        assert posted["request"] == auth_pb.GetTokenRequest(
            client_id="config-client",
            client_secret="config-secret",
            grant_type="client_credentials",
        )
        assert "closed" not in posted
        client.close()
        assert posted["close_count"] == 1
        assert client._env_id == "env-1"

    def test_loads_project_scoped_token_from_chalk_login_config(
        self,
        monkeypatch,
        tmp_path,
    ):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        project_dir = tmp_path / "project"
        work_dir = project_dir / "nested"
        work_dir.mkdir(parents=True)
        (project_dir / "chalk.yml").write_text("project: test\n")
        monkeypatch.chdir(work_dir)
        monkeypatch.delenv("CHALK_API_SERVER", raising=False)
        monkeypatch.delenv("CHALK_CLIENT_ID", raising=False)
        monkeypatch.delenv("CHALK_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("CHALK_ENVIRONMENT", raising=False)
        monkeypatch.delenv("CHALK_ENVIRONMENT_ID", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_dir))
        (config_dir / ".chalk.yml").write_text(
            f"""
tokens:
  default:
    clientId: default-client
    clientSecret: default-secret
    apiServer: https://api.default.chalk.ai
  {str(project_dir)!r}:
    clientId: project-client
    clientSecret: project-secret
    apiServer: https://api.project.chalk.ai
    activeEnvironment: env-project
""",
        )
        posted: dict[str, Any] = {}

        _install_token_client(
            monkeypatch,
            {
                "accessToken": "token",
                "primaryEnvironment": "env-primary",
                "grpcEngines": {"env-project": "engine.chalk.ai:443"},
                "environmentIdToName": {"env-project": "Project"},
            },
            posted,
        )

        ConnectClient()._ensure_auth()

        assert posted["address"] == "https://api.project.chalk.ai"
        assert posted["request"].client_id == "project-client"
        assert posted["request"].client_secret == "project-secret"


class TestConnectClientWebIdentity:
    @pytest.fixture(autouse=True)
    def clear_explicit_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CHALK_ENVIRONMENT", raising=False)
        monkeypatch.delenv("CHALK_ENVIRONMENT_ID", raising=False)

    def test_uses_identity_jwt_without_exchanging_credentials(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        token = _identity_jwt("env-identity")
        token_file = tmp_path / "identity.jwt"
        token_file.write_text(token)
        monkeypatch.setenv("CHALK_WEB_IDENTITY_TOKEN_FILE", str(token_file))
        monkeypatch.setenv("CHALK_API_SERVER", "https://api.identity.chalk.test/")
        monkeypatch.setenv("CHALK_CLIENT_ID", "ignored")
        monkeypatch.setenv("CHALK_CLIENT_SECRET", "ignored")
        monkeypatch.setattr(
            "chalkcompute._connect.resolve_service_auth_context",
            lambda: (_ for _ in ()).throw(
                AssertionError("identity auth must take precedence over service auth")
            ),
        )

        class _UnexpectedAuthClient:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                raise AssertionError("identity JWTs must not be exchanged")

        monkeypatch.setattr(
            "chalkcompute._connect.auth_connect.AuthServiceClientSync",
            _UnexpectedAuthClient,
        )

        client = ConnectClient()
        client._ensure_auth()

        assert client._api_server == "https://api.identity.chalk.test"
        assert client._access_token == token
        assert client._env_id == "env-identity"
        assert client._rpc_metadata() == [
            ("authorization", f"Bearer {token}"),
            ("x-chalk-env-id", "env-identity"),
            ("x-chalk-server", "go-api"),
        ]

    def test_rereads_rotated_token_and_claims_after_one_hour(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monotonic_now = [100.0]
        wall_now = 1_000.0
        monkeypatch.setattr(
            "chalkcompute._connect.time.monotonic", lambda: monotonic_now[0]
        )
        monkeypatch.setattr("chalkcompute._connect.time.time", lambda: wall_now)
        token_file = tmp_path / "identity.jwt"
        first = _identity_jwt(
            "env-one",
            subject="one",
            expires_at=wall_now + 24 * 60 * 60,
        )
        second = _identity_jwt("env-two", subject="two")
        token_file.write_text(first)
        monkeypatch.setenv("CHALK_WEB_IDENTITY_TOKEN_FILE", str(token_file))

        client = ConnectClient()
        first_metadata = client._rpc_metadata()
        token_file.write_text(second)
        monotonic_now[0] += 3599.0
        cached_metadata = client._rpc_metadata()
        monotonic_now[0] += 1.0
        refreshed_metadata = client._rpc_metadata()

        assert ("authorization", f"Bearer {first}") in first_metadata
        assert ("x-chalk-env-id", "env-one") in first_metadata
        assert ("authorization", f"Bearer {first}") in cached_metadata
        assert ("x-chalk-env-id", "env-one") in cached_metadata
        assert ("authorization", f"Bearer {second}") in refreshed_metadata
        assert ("x-chalk-env-id", "env-two") in refreshed_metadata

    def test_cached_metadata_does_not_acquire_client_locks(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        token = _identity_jwt("env-one")
        token_file = tmp_path / "identity.jwt"
        token_file.write_text(token)
        monkeypatch.setenv("CHALK_WEB_IDENTITY_TOKEN_FILE", str(token_file))
        client = ConnectClient()
        expected = client._rpc_metadata()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with client._auth_lock, client._typed_clients_lock:
                future = executor.submit(client._rpc_metadata)
                assert future.result(timeout=1) == expected

    def test_concurrent_refresh_reads_identity_file_once(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monotonic_now = [100.0]
        monkeypatch.setattr(
            "chalkcompute._connect.time.monotonic", lambda: monotonic_now[0]
        )
        token_file = tmp_path / "identity.jwt"
        first = _identity_jwt("env-one", subject="one")
        second = _identity_jwt("env-two", subject="two")
        token_file.write_text(first)
        monkeypatch.setenv("CHALK_WEB_IDENTITY_TOKEN_FILE", str(token_file))
        client = ConnectClient()
        client._rpc_metadata()

        token_file.write_text(second)
        monotonic_now[0] += 60 * 60
        reads = 0
        read_started = threading.Event()
        release_read = threading.Event()
        start = threading.Barrier(4)
        read_token = _web_identity.read_web_identity_token

        def slow_read(path: str):
            nonlocal reads
            reads += 1
            read_started.set()
            assert release_read.wait(timeout=1)
            return read_token(path)

        def metadata() -> list[tuple[str, str]]:
            start.wait(timeout=1)
            return client._rpc_metadata()

        monkeypatch.setattr("chalkcompute._connect.read_web_identity_token", slow_read)
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(metadata) for _ in range(4)]
            try:
                assert read_started.wait(timeout=1)
            finally:
                release_read.set()
            results = [future.result(timeout=1) for future in futures]

        assert reads == 1
        assert all(("authorization", f"Bearer {second}") in value for value in results)

    def test_refreshes_at_expiry_window_without_halving_remaining_lifetime(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monotonic_now = [100.0]
        wall_now = 1_000.0
        monkeypatch.setattr(
            "chalkcompute._connect.time.monotonic", lambda: monotonic_now[0]
        )
        monkeypatch.setattr("chalkcompute._connect.time.time", lambda: wall_now)
        token_file = tmp_path / "identity.jwt"
        first = _identity_jwt(
            "env-one",
            subject="one",
            expires_at=wall_now + 30 * 60,
        )
        second = _identity_jwt("env-two", subject="two")
        token_file.write_text(first)
        monkeypatch.setenv("CHALK_WEB_IDENTITY_TOKEN_FILE", str(token_file))

        client = ConnectClient()
        assert ("authorization", f"Bearer {first}") in client._rpc_metadata()
        token_file.write_text(second)
        monotonic_now[0] += 1499.0
        assert ("authorization", f"Bearer {first}") in client._rpc_metadata()
        monotonic_now[0] += 1.0
        assert ("authorization", f"Bearer {second}") in client._rpc_metadata()

    def test_auth_configuration_is_fixed_after_first_use(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        first_file = tmp_path / "first.jwt"
        second_file = tmp_path / "second.jwt"
        first = _identity_jwt("env-one", subject="one")
        second = _identity_jwt("env-two", subject="two")
        first_file.write_text(first)
        second_file.write_text(second)
        monkeypatch.setenv("CHALK_WEB_IDENTITY_TOKEN_FILE", str(first_file))
        monkeypatch.setenv("CHALK_API_SERVER", "https://api.one.chalk.test")

        client = ConnectClient()
        assert ("authorization", f"Bearer {first}") in client._rpc_metadata()

        monkeypatch.setenv("CHALK_WEB_IDENTITY_TOKEN_FILE", str(second_file))
        monkeypatch.setenv("CHALK_API_SERVER", "https://api.two.chalk.test")

        assert ("authorization", f"Bearer {first}") in client._rpc_metadata()
        assert client._api_server == "https://api.one.chalk.test"

        replacement = ConnectClient()
        assert ("authorization", f"Bearer {second}") in replacement._rpc_metadata()
        assert replacement._api_server == "https://api.two.chalk.test"

    def test_rejects_expired_token_and_bounds_file_retries(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monotonic_now = [100.0]
        wall_now = 1_000.0
        monkeypatch.setattr(
            "chalkcompute._connect.time.monotonic", lambda: monotonic_now[0]
        )
        monkeypatch.setattr("chalkcompute._connect.time.time", lambda: wall_now)
        token_file = tmp_path / "identity.jwt"
        token_file.write_text(_identity_jwt("env-one", expires_at=wall_now - 1))
        monkeypatch.setenv("CHALK_WEB_IDENTITY_TOKEN_FILE", str(token_file))

        reads = 0
        read_token = _web_identity.read_web_identity_token

        def counting_read(path: str):
            nonlocal reads
            reads += 1
            return read_token(path)

        monkeypatch.setattr(
            "chalkcompute._connect.read_web_identity_token", counting_read
        )
        client = ConnectClient()

        for _ in range(3):
            with pytest.raises(ConnectError, match="has expired"):
                client._rpc_metadata()
        assert reads == 1

        monotonic_now[0] += 5.0
        with pytest.raises(ConnectError, match="has expired"):
            client._rpc_metadata()
        assert reads == 2

    def test_explicit_environment_overrides_jwt_claim(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        token_file = tmp_path / "identity.jwt"
        token_file.write_text(_identity_jwt("env-from-jwt"))
        monkeypatch.setenv("CHALK_WEB_IDENTITY_TOKEN_FILE", str(token_file))
        monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "env-explicit")

        client = ConnectClient()
        client._ensure_auth()

        assert client._env_id == "env-explicit"

    def test_rejects_invalid_exp_claim(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        token_file = tmp_path / "identity.jwt"
        token_file.write_text(
            _identity_jwt("env-identity", expires_at="not-a-numeric-date")
        )
        monkeypatch.setenv("CHALK_WEB_IDENTITY_TOKEN_FILE", str(token_file))

        with pytest.raises(ConnectError, match="invalid exp claim"):
            ConnectClient()._ensure_auth()

    @pytest.mark.parametrize(
        ("contents", "message"),
        [
            ("", "is empty"),
            ("not-a-jwt", "is malformed"),
            (
                "e30.bm90LWpzb24.signature",
                "invalid payload",
            ),
        ],
    )
    def test_reports_invalid_identity_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
        contents: str,
        message: str,
    ) -> None:
        token_file = tmp_path / "identity.jwt"
        token_file.write_text(contents)
        monkeypatch.setenv("CHALK_WEB_IDENTITY_TOKEN_FILE", str(token_file))

        with pytest.raises(ConnectError, match=message):
            ConnectClient()._ensure_auth()

    def test_uses_explicit_grpc_engine(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        token_file = tmp_path / "identity.jwt"
        token_file.write_text(_identity_jwt("env-identity"))
        monkeypatch.setenv("CHALK_WEB_IDENTITY_TOKEN_FILE", str(token_file))
        monkeypatch.setenv("CHALK_GRPC_ENGINE", "localhost:50051")

        assert ConnectClient().grpc_engine_target() == ("localhost:50051", False)

    def test_requires_grpc_engine_for_queued_calls(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        token_file = tmp_path / "identity.jwt"
        token_file.write_text(_identity_jwt("env-identity"))
        monkeypatch.setenv("CHALK_WEB_IDENTITY_TOKEN_FILE", str(token_file))
        monkeypatch.delenv("CHALK_GRPC_ENGINE", raising=False)

        with pytest.raises(ConnectError, match="CHALK_GRPC_ENGINE must be set"):
            ConnectClient().grpc_engine_target()

    def test_sandbox_metadata_observes_rotation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monotonic_now = [100.0]
        monkeypatch.setattr(
            "chalkcompute._connect.time.monotonic", lambda: monotonic_now[0]
        )
        token_file = tmp_path / "identity.jwt"
        first = _identity_jwt("env-one", subject="one")
        second = _identity_jwt("env-two", subject="two")
        token_file.write_text(first)
        monkeypatch.setenv("CHALK_WEB_IDENTITY_TOKEN_FILE", str(token_file))
        monkeypatch.setenv("CHALK_API_SERVER", "http://localhost:18080")

        client = SandboxClient.from_env()
        try:
            assert ("authorization", f"Bearer {first}") in (client._metadata or [])
            token_file.write_text(second)
            assert client._connect is not None
            monotonic_now[0] += 60 * 60
            assert ("authorization", f"Bearer {second}") in (client._metadata or [])
            assert ("x-chalk-env-id", "env-two") in (client._metadata or [])
        finally:
            client.close()


class TestConnectClientWorkloadIdentityToken:
    def test_requests_token_for_audience(self, monkeypatch: pytest.MonkeyPatch) -> None:
        request: dict[str, Any] = {}
        client = ConnectClient()

        stub = types.SimpleNamespace(get_workload_identity_token=object())
        monkeypatch.setattr(client, "stub", lambda _cls: stub)

        def fake_call(
            method: object, body: Any, *, timeout: float = 30, **_kwargs: Any
        ):
            request.update(
                method=method,
                body=body,
                timeout=timeout,
            )
            return auth_pb.GetWorkloadIdentityTokenResponse(token="snowflake-jwt")

        monkeypatch.setattr(client, "call", fake_call)

        token = client.get_workload_identity_token(
            "snowflakecomputing.com",
            timeout=12,
        )

        assert token == "snowflake-jwt"
        assert request == {
            "method": stub.get_workload_identity_token,
            "body": auth_pb.GetWorkloadIdentityTokenRequest(
                audience="snowflakecomputing.com"
            ),
            "timeout": 12,
        }

    @pytest.mark.parametrize("audience", ["", "   "])
    def test_rejects_empty_audience(self, audience: str) -> None:
        with pytest.raises(ValueError, match="audience cannot be empty"):
            ConnectClient().get_workload_identity_token(audience)

    @pytest.mark.parametrize("payload", [{}, {"token": ""}, {"token": 123}])
    def test_rejects_response_without_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
        payload: dict[str, Any],
    ) -> None:
        client = ConnectClient()
        monkeypatch.setattr(
            client,
            "stub",
            lambda _cls: types.SimpleNamespace(get_workload_identity_token=object()),
        )
        monkeypatch.setattr(
            client,
            "call",
            lambda *_args, **_kwargs: types.SimpleNamespace(token=payload.get("token")),
        )

        with pytest.raises(ConnectError, match="response without a token"):
            client.get_workload_identity_token("snowflakecomputing.com")


class TestConnectClientAuthRefresh:
    def test_retains_previous_token_only_until_its_hard_expiration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monotonic_now = [100.0]
        monkeypatch.setattr(
            "chalkcompute._connect.time.monotonic", lambda: monotonic_now[0]
        )
        state = _connect._AuthState(
            api_server="https://api.chalk.test",
            access_token="current-token",
            environment_id="env-1",
            grpc_engines={},
            environment_id_to_name={},
        )

        class _Provider:
            uses_web_identity = False

            def __init__(self) -> None:
                self.calls = 0

            def refresh(self) -> _connect._AuthCache:
                self.calls += 1
                if self.calls > 1:
                    raise ConnectError("auth unavailable")
                return _connect._AuthCache(
                    state=state,
                    refresh_after=200.0,
                    expires_after=300.0,
                )

        provider = _Provider()
        client = ConnectClient()
        client._auth_provider = provider

        assert client._auth_state() is state
        monotonic_now[0] = 200.0
        assert client._auth_state() is state
        assert provider.calls == 2
        monotonic_now[0] = 204.0
        assert client._auth_state() is state
        assert provider.calls == 2
        monotonic_now[0] = 300.0
        with pytest.raises(ConnectError, match="auth unavailable"):
            client._auth_state()
        assert provider.calls == 3


class TestConnectClientTracing:
    def test_trace_headers_return_current_w3c_context(self, monkeypatch):
        monkeypatch.setattr(
            _tracing,
            "current_trace_metadata",
            lambda: [("traceparent", "00-trace-span-01")],
        )

        assert _tracing.trace_headers() == {"traceparent": "00-trace-span-01"}

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

    def test_span_defaults_to_sample_rate_one_when_unconfigured(self, monkeypatch):
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

        with _tracing.span("workflow"):
            pass

        assert rates == [1.0]
        assert tracer.span_names == ["workflow"]

    def test_span_leaves_exception_recording_to_otel_context_manager(self, monkeypatch):
        tracer = _Tracer()
        monkeypatch.setattr(_tracing, "_initialized", False)
        monkeypatch.setattr(_tracing, "_provider", None)
        monkeypatch.setattr(_tracing, "_user_tracing_configured", False)
        monkeypatch.setattr(_tracing, "_install_tracer_provider", lambda _rate: None)
        monkeypatch.setattr(_tracing, "get_tracer", lambda: tracer)

        with pytest.raises(RuntimeError, match="boom"):
            with _tracing.span("workflow"):
                raise RuntimeError("boom")

        assert tracer.spans[0].recorded_exceptions == []
        assert tracer.spans[0].statuses == []

    def test_span_noops_when_remote_tracing_is_off(self, monkeypatch):
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

        with _tracing.span("workflow") as span:
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
        monkeypatch.setattr(client, "_ensure_auth", lambda: None)
        return client

    def test_call_does_not_inject_trace_metadata(self, monkeypatch):
        posted: dict[str, Any] = {}

        def fake_method(_request, **kwargs):
            posted["kwargs"] = kwargs
            return object()

        def fail_current_trace_metadata():
            raise AssertionError(
                "trace metadata should not be injected without upstream trace"
            )

        monkeypatch.setattr(
            "chalkcompute._tracing.current_trace_metadata",
            fail_current_trace_metadata,
        )
        client = self._client(monkeypatch)
        monkeypatch.setattr(
            client, "_rpc_metadata", lambda: [("authorization", "Bearer token")]
        )
        client.call(fake_method, object())

        assert posted["kwargs"]["headers"] == {"authorization": "Bearer token"}

    def test_call_merges_extra_headers(self, monkeypatch):
        posted: dict[str, Any] = {}

        def fake_method(_request, **kwargs):
            posted["kwargs"] = kwargs
            return object()

        client = self._client(monkeypatch)
        monkeypatch.setattr(
            client, "_rpc_metadata", lambda: [("authorization", "Bearer token")]
        )
        client.call(
            fake_method,
            object(),
            extra_headers={"x-chalk-test-header": "embed"},
        )

        assert posted["kwargs"]["headers"] == {
            "authorization": "Bearer token",
            "x-chalk-test-header": "embed",
        }


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
        _install_auth_client(monkeypatch, {"accessToken": "working-token"}, posted)

        client = ConnectClient()
        client._ensure_auth()

        assert posted["address"] == "https://api.chalk.test"
        assert posted["request"] == auth_pb.GetInternalWorkingTokenRequest(
            requested_permissions=[2, 3, 11]
        )
        assert (
            posted["call_kwargs"]["headers"]["Authorization"] == "Bearer exchange-token"
        )
        assert "closed" not in posted
        assert client._rpc_metadata()[:2] == [
            ("authorization", "Bearer working-token"),
            ("x-chalk-env-id", "env-1"),
        ]
        client.close()
        client.close()
        assert posted["close_count"] == 1

    def test_uses_grpc_engine_from_exchange_token_response(self, monkeypatch):
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

        _install_auth_client(
            monkeypatch,
            {
                "accessToken": "working-token",
                "grpcEngines": {"env-1": "engine.chalk.test:443"},
                "environmentIdToName": {"env-1": "Staging"},
            },
            {},
        )

        target, use_tls = ConnectClient().grpc_engine_target()

        assert target == "engine.chalk.test:443"
        assert use_tls is True

    def test_refreshes_with_one_auth_client_and_rotated_exchange_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monotonic_now = [100.0]
        monkeypatch.setattr(
            "chalkcompute._connect.time.monotonic", lambda: monotonic_now[0]
        )
        monkeypatch.setattr(
            "chalkcompute._connect.time.time",
            lambda: 1_000.0 + monotonic_now[0] - 100.0,
        )
        resolved = 0

        def resolve() -> ServiceAuthContext:
            nonlocal resolved
            resolved += 1
            return ServiceAuthContext(
                auth_endpoint="https://api.chalk.test",
                exchange_token=f"exchange-token-{resolved}",
                environment_id="env-1",
                requested_permissions=(2, 3, 11),
                grpc_engine=None,
            )

        monkeypatch.setattr(
            "chalkcompute._connect.resolve_service_auth_context", resolve
        )
        captured: dict[str, Any] = {"authorizations": []}

        class _AuthClient:
            def __init__(self, _address: str, **_kwargs: Any) -> None:
                captured["init_count"] = captured.get("init_count", 0) + 1

            def get_internal_working_token(
                self, _request: Any, **kwargs: Any
            ) -> auth_pb.GetInternalWorkingTokenResponse:
                captured["authorizations"].append(kwargs["headers"]["Authorization"])
                call_count = len(captured["authorizations"])
                expires_at = (
                    "1970-01-01T00:26:40Z"
                    if call_count == 1
                    else "1970-01-01T00:36:40Z"
                )
                return auth_pb.GetInternalWorkingTokenResponse.from_json(
                    json.dumps(
                        {
                            "accessToken": f"working-token-{call_count}",
                            "expiresAt": expires_at,
                        }
                    )
                )

            def close(self) -> None:
                captured["close_count"] = captured.get("close_count", 0) + 1

        monkeypatch.setattr(
            "chalkcompute._connect.auth_connect.AuthServiceClientSync", _AuthClient
        )
        client = ConnectClient()

        assert ("authorization", "Bearer working-token-1") in client._rpc_metadata()
        monotonic_now[0] += 299.0
        assert ("authorization", "Bearer working-token-1") in client._rpc_metadata()
        monotonic_now[0] += 1.0
        assert ("authorization", "Bearer working-token-2") in client._rpc_metadata()
        assert client.stub(auth_connect.AuthServiceClientSync) is client.stub(
            auth_connect.AuthServiceClientSync
        )
        assert resolved == 2
        assert captured["init_count"] == 1
        assert captured["authorizations"] == [
            "Bearer exchange-token-1",
            "Bearer exchange-token-2",
        ]
        assert "close_count" not in captured

        client.close()
        client.close()
        assert captured["close_count"] == 1
        assert client._access_token is None
        with pytest.raises(ConnectError, match="closed"):
            client._rpc_metadata()


class TestConnectClientCredentialLifecycle:
    def test_captures_config_once_and_reuses_auth_client_across_refreshes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monotonic_now = [100.0]
        monkeypatch.setattr(
            "chalkcompute._connect.time.monotonic", lambda: monotonic_now[0]
        )
        monkeypatch.setattr(
            "chalkcompute._connect.time.time",
            lambda: 1_000.0 + monotonic_now[0] - 100.0,
        )
        config_loads = 0

        def load_config(*_args: Any) -> None:
            nonlocal config_loads
            config_loads += 1
            return None

        monkeypatch.setattr("chalkcompute._connect.load_token", load_config)
        monkeypatch.setattr(
            "chalkcompute._connect.resolve_service_auth_context", lambda: None
        )
        monkeypatch.setenv("CHALK_API_SERVER", "https://api.chalk.test/")
        monkeypatch.setenv("CHALK_CLIENT_ID", "client-id-1")
        monkeypatch.setenv("CHALK_CLIENT_SECRET", "client-secret-1")
        monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "env-1")
        captured: dict[str, Any] = {"requests": []}

        class _AuthClient:
            def __init__(self, address: str, **_kwargs: Any) -> None:
                captured["init_count"] = captured.get("init_count", 0) + 1
                captured["address"] = address

            def get_token(
                self, request: Any, **_kwargs: Any
            ) -> auth_pb.GetTokenResponse:
                captured["requests"].append(request)
                return auth_pb.GetTokenResponse(
                    access_token=f"access-token-{len(captured['requests'])}",
                    expires_in=600,
                )

            def close(self) -> None:
                captured["close_count"] = captured.get("close_count", 0) + 1

        monkeypatch.setattr(
            "chalkcompute._connect.auth_connect.AuthServiceClientSync", _AuthClient
        )
        client = ConnectClient()

        assert ("authorization", "Bearer access-token-1") in client._rpc_metadata()
        monkeypatch.setenv("CHALK_CLIENT_ID", "client-id-2")
        monkeypatch.setenv("CHALK_CLIENT_SECRET", "client-secret-2")
        monotonic_now[0] += 300.0
        assert ("authorization", "Bearer access-token-2") in client._rpc_metadata()

        assert config_loads == 1
        assert captured["address"] == "https://api.chalk.test"
        assert captured["init_count"] == 1
        assert [request.client_id for request in captured["requests"]] == [
            "client-id-1",
            "client-id-1",
        ]
        client.close()
        assert captured["close_count"] == 1


class _FakeChalkClient:
    calls: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.session = types.SimpleNamespace(auth=None)
        self.__class__.calls.append(kwargs)


class TestChalkClientFromEnv:
    @pytest.fixture(autouse=True)
    def fake_chalkpy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _FakeChalkClient.calls = []
        chalk_mod = types.ModuleType("chalk")
        client_mod = types.ModuleType("chalk.client")
        client_mod.ChalkClient = _FakeChalkClient
        chalk_mod.client = client_mod
        monkeypatch.setitem(sys.modules, "chalk", chalk_mod)
        monkeypatch.setitem(sys.modules, "chalk.client", client_mod)

    def test_uses_service_auth_for_chalk_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
        _install_auth_client(monkeypatch, {"accessToken": "working-token"}, posted)

        client = chalk_client_from_env()

        assert isinstance(client, _FakeChalkClient)
        assert posted["address"] == "https://api.chalk.test"
        assert _FakeChalkClient.calls == [
            {
                "api_server": "https://api.chalk.test",
                "environment": "env-1",
                "additional_headers": {"Authorization": "Bearer working-token"},
            }
        ]
        request = types.SimpleNamespace(headers={})
        client.session.auth(request)
        assert request.headers["Authorization"] == "Bearer working-token"
        assert posted["init_count"] == 1

    def test_respects_explicit_authorization_header(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
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

        class _UnexpectedAuthClient:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                raise AssertionError("explicit authorization must not be exchanged")

        monkeypatch.setattr(
            "chalkcompute._connect.auth_connect.AuthServiceClientSync",
            _UnexpectedAuthClient,
        )

        client = chalk_client_from_env(
            additional_headers={"Authorization": "Bearer explicit-token"},
            environment="user-env",
        )

        assert isinstance(client, _FakeChalkClient)
        assert _FakeChalkClient.calls == [
            {
                "additional_headers": {"Authorization": "Bearer explicit-token"},
                "environment": "user-env",
            }
        ]

    def test_falls_back_without_service_auth(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "chalkcompute._connect.resolve_service_auth_context",
            lambda: None,
        )

        client = chalk_client_from_env(api_server="https://api.local.chalk.test")

        assert isinstance(client, _FakeChalkClient)
        assert _FakeChalkClient.calls == [
            {
                "api_server": "https://api.local.chalk.test",
                "additional_headers": None,
            }
        ]


class TestSandboxClientFromEnv:
    def test_uses_insecure_address_for_local_http_api(self, monkeypatch):
        monkeypatch.setenv("CHALK_API_SERVER", "http://localhost:18080")
        monkeypatch.setenv("CHALK_CLIENT_ID", "client-id")
        monkeypatch.setenv("CHALK_CLIENT_SECRET", "client-secret")
        monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "env-1")
        _install_token_client(
            monkeypatch,
            {
                "accessToken": "token",
                "primaryEnvironment": "env-1",
                "environmentIdToName": {"env-1": "Local"},
            },
        )

        client = SandboxClient.from_env()
        try:
            assert client._target == "localhost:18080"
        finally:
            client.close()


class TestStubAndCall:
    """The ConnectRPC transport seam: ConnectClient.stub() + ConnectClient.call()."""

    def _authed_client(self) -> ConnectClient:
        client = ConnectClient()
        client._access_token = "tok1"
        client._env_id = "env-1"
        client._api_server = "http://localhost:50051"
        return client

    def test_call_rebuilds_metadata_per_call(self, monkeypatch) -> None:
        monkeypatch.delenv("CHALK_WEB_IDENTITY_TOKEN_FILE", raising=False)
        client = self._authed_client()
        captured: list[tuple[Any, Any]] = []

        def method(request: Any, timeout_ms: Any = None, headers: Any = None) -> str:
            captured.append((timeout_ms, headers))
            return "resp"

        assert client.call(method, object(), timeout=12) == "resp"
        client._access_token = "tok2"  # simulate token rotation
        client.call(method, object())

        assert captured[0][0] == 12_000
        assert captured[0][1]["authorization"] == "Bearer tok1"
        assert captured[1][1]["authorization"] == "Bearer tok2"
        assert captured[1][1]["x-chalk-env-id"] == "env-1"

    def test_stub_uses_grpc_protocol(self, monkeypatch) -> None:
        from connectrpc.protocol import ProtocolType

        from chalkcompute._rpc import RPC_READ_MAX_BYTES

        monkeypatch.delenv("CHALK_WEB_IDENTITY_TOKEN_FILE", raising=False)
        captured: dict[str, object] = {}

        class Stub:
            def __init__(
                self,
                address: str,
                *,
                protocol: ProtocolType,
                interceptors: tuple,
                http_client: object,
                read_max_bytes: int,
            ) -> None:
                captured.update(
                    address=address,
                    protocol=protocol,
                    interceptor_count=len(interceptors),
                    http_client=http_client,
                    read_max_bytes=read_max_bytes,
                )

        client = ConnectClient()
        client._access_token = "tok"
        client._env_id = "env-1"
        client._api_server = "http://localhost:50051"
        assert isinstance(client.stub(Stub), Stub)
        assert captured == {
            "address": "http://localhost:50051",
            "protocol": ProtocolType.GRPC,
            "interceptor_count": 2,
            "http_client": SYNC_HTTP_CLIENT,
            "read_max_bytes": RPC_READ_MAX_BYTES,
        }
