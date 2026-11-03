from __future__ import annotations

import os

import pytest

from chalkcompute import ConnectClient
from chalkcompute import _auth_config
from tests.test_connect import _identity_jwt, _install_token_client


@pytest.fixture
def login(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith(("CHALK_", "_CHALK_")):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / ".chalk.yml").write_text(
        "tokens:\n  default:\n    clientId: local-id\n"
        "    clientSecret: local-secret\n    apiServer: https://api.example.com/\n"
        "    activeEnvironment: Production\n"
    )
    monkeypatch.setattr(
        "chalkcompute._connect.resolve_service_auth_context", lambda: None
    )
    _auth_config._load_global_config.cache_clear()
    captured = {}
    _install_token_client(
        monkeypatch,
        {"accessToken": "bearer", "environmentIdToName": {"env-1": "Production"}},
        captured,
    )
    yield captured
    _auth_config._load_global_config.cache_clear()


def test_public_context_resolves_login_lazily_without_environment_exports(login):
    before = dict(os.environ)
    with ConnectClient() as client:
        assert not login
        assert client.api_server == "https://api.example.com"
        assert client.environment_id == "env-1"
        headers = client.request_headers()
        assert headers["authorization"] == "Bearer bearer"
        assert headers["x-chalk-env-id"] == "env-1"
        headers["authorization"] = "changed"
        assert client.request_headers()["authorization"] == "Bearer bearer"
    assert dict(os.environ) == before
    assert login["closed"] is True


def test_public_request_headers_follow_workload_identity_rotation(
    monkeypatch, tmp_path
):
    path = tmp_path / "identity"
    path.write_text(_identity_jwt("env-workload", subject="first"))
    monkeypatch.setenv("CHALK_WEB_IDENTITY_TOKEN_FILE", str(path))
    monkeypatch.setenv("CHALK_API_SERVER", "https://workload.example.com")
    monkeypatch.delenv("CHALK_ENVIRONMENT", raising=False)
    monkeypatch.delenv("CHALK_ENVIRONMENT_ID", raising=False)
    now = [0.0]
    monkeypatch.setattr("chalkcompute._connect.time.monotonic", lambda: now[0])
    with ConnectClient() as client:
        assert client.environment_id == "env-workload"
        assert client.api_server == "https://workload.example.com"
        first = client.request_headers()["authorization"]
        path.write_text(_identity_jwt("env-workload", subject="second"))
        now[0] = 3601
        assert client.request_headers()["authorization"] != first


def test_context_manager_closes_on_exception(monkeypatch):
    client = ConnectClient()
    closed = []
    monkeypatch.setattr(client, "close", lambda: closed.append(True))
    with pytest.raises(RuntimeError):
        with client:
            raise RuntimeError("caller failed")
    assert closed == [True]
