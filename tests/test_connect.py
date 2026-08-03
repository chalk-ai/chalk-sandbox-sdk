import contextlib
import base64
import json
import sys
import time
import types
from typing import Any

import pytest

import chalkcompute._auth_config as _auth_config
from chalkcompute import SandboxClient, _tracing
from chalkcompute._connect import (
    ConnectError,
    ConnectClient,
    chalk_client_from_env,
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
        monkeypatch.setattr(
            "chalkcompute._http.post",
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
            "chalkcompute._http.post",
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

        def fake_post(*args, **kwargs):
            posted["args"] = args
            posted["kwargs"] = kwargs
            return _TokenResponse(
                {
                    "access_token": "token",
                    "primary_environment": "env-primary",
                    "grpc_engines": {"env-1": "engine.staging.chalk.dev:443"},
                    "environment_id_to_name": {"env-1": "Staging"},
                }
            )

        monkeypatch.setattr("chalkcompute._http.post", fake_post)

        client = ConnectClient()
        client._ensure_auth()

        assert posted["args"] == ("https://api.config.chalk.ai/v1/oauth/token",)
        assert posted["kwargs"]["json"] == {
            "client_id": "config-client",
            "client_secret": "config-secret",
            "grant_type": "client_credentials",
        }
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

        def fake_post(*args, **kwargs):
            posted["args"] = args
            posted["kwargs"] = kwargs
            return _TokenResponse(
                {
                    "access_token": "token",
                    "primary_environment": "env-primary",
                    "grpc_engines": {"env-project": "engine.chalk.ai:443"},
                    "environment_id_to_name": {"env-project": "Project"},
                }
            )

        monkeypatch.setattr("chalkcompute._http.post", fake_post)

        ConnectClient()._ensure_auth()

        assert posted["args"] == ("https://api.project.chalk.ai/v1/oauth/token",)
        assert posted["kwargs"]["json"]["client_id"] == "project-client"
        assert posted["kwargs"]["json"]["client_secret"] == "project-secret"


class TestConnectClientWebIdentity:
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
        monkeypatch.setattr(
            "chalkcompute._http.post",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("identity JWTs must not be exchanged")
            ),
        )

        client = ConnectClient()
        client._ensure_auth()

        assert client._api_server == "https://api.identity.chalk.test"
        assert client._access_token == token
        assert client._env_id == "env-identity"
        assert client._headers == {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "X-Chalk-Env-Id": "env-identity",
            "X-Chalk-Server": "go-api",
        }

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
        first_metadata = client._grpc_metadata()
        token_file.write_text(second)
        monotonic_now[0] += 3599.0
        cached_metadata = client._grpc_metadata()
        monotonic_now[0] += 1.0
        refreshed_metadata = client._grpc_metadata()

        assert ("authorization", f"Bearer {first}") in first_metadata
        assert ("x-chalk-env-id", "env-one") in first_metadata
        assert ("authorization", f"Bearer {first}") in cached_metadata
        assert ("x-chalk-env-id", "env-one") in cached_metadata
        assert ("authorization", f"Bearer {second}") in refreshed_metadata
        assert ("x-chalk-env-id", "env-two") in refreshed_metadata

    def test_cache_uses_half_of_remaining_token_lifetime(
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
        assert ("authorization", f"Bearer {first}") in client._grpc_metadata()
        token_file.write_text(second)
        monotonic_now[0] += 899.0
        assert ("authorization", f"Bearer {first}") in client._grpc_metadata()
        monotonic_now[0] += 1.0
        assert ("authorization", f"Bearer {second}") in client._grpc_metadata()

    def test_changing_identity_file_path_bypasses_cache(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        first_file = tmp_path / "first.jwt"
        second_file = tmp_path / "second.jwt"
        first = _identity_jwt("env-one", subject="one")
        second = _identity_jwt("env-two", subject="two")
        first_file.write_text(first)
        second_file.write_text(second)
        monkeypatch.setenv("CHALK_WEB_IDENTITY_TOKEN_FILE", str(first_file))

        client = ConnectClient()
        assert ("authorization", f"Bearer {first}") in client._grpc_metadata()

        monkeypatch.setenv("CHALK_WEB_IDENTITY_TOKEN_FILE", str(second_file))

        assert ("authorization", f"Bearer {second}") in client._grpc_metadata()
        assert ("x-chalk-env-id", "env-two") in client._grpc_metadata()

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
            assert client._connect._web_identity_cached_at is not None
            client._connect._web_identity_cached_at -= 60 * 60
            assert ("authorization", f"Bearer {second}") in (client._metadata or [])
            assert ("x-chalk-env-id", "env-two") in (client._metadata or [])
        finally:
            client.close()


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
        monkeypatch.setattr("chalkcompute._http.post", fake_post)

        self._client(monkeypatch).rpc("svc", "Method", {})

        assert posted["kwargs"]["headers"] == {"Authorization": "Bearer token"}

    def test_rpc_merges_extra_headers_without_mutating_cached_headers(
        self, monkeypatch
    ):
        posted: dict[str, Any] = {}

        def fake_post(*args, **kwargs):
            posted["kwargs"] = kwargs
            return _RPCResponse()

        monkeypatch.setattr("chalkcompute._http.post", fake_post)

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

        monkeypatch.setattr("chalkcompute._http.post", fake_post)

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

        def fake_post(*_args, **_kwargs):
            return _TokenResponse(
                {
                    "accessToken": "working-token",
                    "grpcEngines": {"env-1": "engine.chalk.test:443"},
                    "environmentIdToName": {"env-1": "Staging"},
                }
            )

        monkeypatch.setattr("chalkcompute._http.post", fake_post)

        target, use_tls = ConnectClient().grpc_engine_target()

        assert target == "engine.chalk.test:443"
        assert use_tls is True


class _FakeChalkClient:
    calls: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
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

        def fake_post(*args: Any, **kwargs: Any) -> _TokenResponse:
            posted["args"] = args
            posted["kwargs"] = kwargs
            return _TokenResponse({"accessToken": "working-token"})

        monkeypatch.setattr("chalkcompute._http.post", fake_post)

        client = chalk_client_from_env()

        assert isinstance(client, _FakeChalkClient)
        assert posted["args"] == (
            "https://api.chalk.test/chalk.server.v1.AuthService/GetInternalWorkingToken",
        )
        assert _FakeChalkClient.calls == [
            {
                "api_server": "https://api.chalk.test",
                "environment": "env-1",
                "additional_headers": {"Authorization": "Bearer working-token"},
            }
        ]

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
        posted = {"called": False}

        def fake_post(*_args: Any, **_kwargs: Any) -> _TokenResponse:
            posted["called"] = True
            return _TokenResponse({"accessToken": "working-token"})

        monkeypatch.setattr("chalkcompute._http.post", fake_post)

        client = chalk_client_from_env(
            additional_headers={"Authorization": "Bearer explicit-token"},
            environment="user-env",
        )

        assert isinstance(client, _FakeChalkClient)
        assert posted["called"] is False
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
    def test_uses_insecure_channel_for_local_http_api(self, monkeypatch):
        monkeypatch.setenv("CHALK_API_SERVER", "http://localhost:18080")
        monkeypatch.setenv("CHALK_CLIENT_ID", "client-id")
        monkeypatch.setenv("CHALK_CLIENT_SECRET", "client-secret")
        monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "env-1")
        monkeypatch.setattr(
            "chalkcompute._http.post",
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


class TestConnectClientAuthConcurrency:
    """ConnectClient is driven from thread pools; _ensure_auth must be safe."""

    @pytest.fixture(autouse=True)
    def disable_service_auth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "chalkcompute._connect.resolve_service_auth_context", lambda: None
        )
        monkeypatch.delenv("CHALK_WEB_IDENTITY_TOKEN_FILE", raising=False)
        monkeypatch.setenv("CHALK_API_SERVER", "https://api.staging.chalk.dev")
        monkeypatch.setenv("CHALK_CLIENT_ID", "client-id")
        monkeypatch.setenv("CHALK_CLIENT_SECRET", "client-secret")
        monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "env-1")

    @staticmethod
    def _payload() -> dict[str, Any]:
        return {
            "access_token": "token",
            "primary_environment": "env-1",
            "environment_id_to_name": {"env-1": "Staging"},
        }

    def test_concurrent_first_use_exchanges_one_token(self, monkeypatch):
        import threading

        exchanges: list[int] = []
        start = threading.Barrier(8)

        def fake_post(*args, **kwargs):
            exchanges.append(1)
            time.sleep(0.02)  # widen the window a racing thread could hit
            return _TokenResponse(self._payload())

        monkeypatch.setattr("chalkcompute._http.post", fake_post)
        client = ConnectClient()
        errors: list[BaseException] = []

        def authenticate() -> None:
            try:
                start.wait(timeout=5)
                client._ensure_auth()
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=authenticate) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert errors == []
        assert len(exchanges) == 1
        assert client._access_token == "token"

    def test_a_visible_token_always_has_its_headers(self, monkeypatch):
        import threading

        monkeypatch.setattr(
            "chalkcompute._http.post",
            lambda *args, **kwargs: _TokenResponse(self._payload()),
        )
        client = ConnectClient()
        observations: list[tuple[object, object]] = []
        stop = threading.Event()

        def reader() -> None:
            while not stop.is_set():
                token, headers = client._access_token, client._headers
                if token is not None:
                    observations.append((token, headers))

        watcher = threading.Thread(target=reader, daemon=True)
        watcher.start()
        client._ensure_auth()
        stop.set()
        watcher.join(timeout=5)

        # _access_token is written last, so seeing it implies the rest landed.
        assert all(
            headers is not None and headers["Authorization"] == f"Bearer {token}"
            for token, headers in observations
        )
