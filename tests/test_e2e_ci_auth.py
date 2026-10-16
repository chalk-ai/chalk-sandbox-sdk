from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from e2etests import conftest


@pytest.fixture
def ci_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUILDKITE", "true")
    monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "env-ci")
    for name in (
        "CHALK_ENVIRONMENT",
        "CHALK_ACTIVE_ENVIRONMENT",
        "_CHALK_ACTIVE_ENVIRONMENT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CHALK_WEB_IDENTITY_TOKEN_FILE", raising=False)
    monkeypatch.setattr(conftest, "CHALK_CLIENT_ID", "client-id")
    monkeypatch.setattr(conftest, "CHALK_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(conftest, "CHALK_API_SERVER", "https://api.example.test")
    monkeypatch.setattr(conftest, "CHALK_ENVIRONMENT_ID", "env-ci")


@pytest.mark.parametrize("buildkite", ["true", "false"])
def test_token_exchange_is_scoped_only_on_buildkite(
    ci_credentials: None, monkeypatch: pytest.MonkeyPatch, buildkite: str
) -> None:
    monkeypatch.setenv("BUILDKITE", buildkite)
    requests: list[dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> Any:
        requests.append(kwargs["content"])
        return SimpleNamespace(status=200, json=lambda: {"access_token": "token"})

    monkeypatch.setattr(conftest, "_HTTP_CLIENT", SimpleNamespace(post=post))

    assert conftest._get_access_token() == "token"
    assert requests[0]["client_id"] == "client-id"
    assert requests[0]["grant_type"] == "client_credentials"
    if buildkite == "true":
        assert requests[0]["scope"] == "env:env-ci"
    else:
        assert "scope" not in requests[0]


def test_ci_token_exchange_requires_an_environment(
    ci_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conftest, "CHALK_ENVIRONMENT_ID", "")
    monkeypatch.setattr(
        conftest,
        "_HTTP_CLIENT",
        SimpleNamespace(post=lambda *a, **kw: pytest.fail("unexpected HTTP call")),
    )

    with pytest.raises(RuntimeError, match="CHALK_ENVIRONMENT_ID"):
        conftest._get_access_token()


def test_ci_identity_is_used_by_sdk_and_inherited_by_subprocesses(
    ci_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chalkcompute._connect import ConnectClient

    payload = (
        base64.urlsafe_b64encode(
            json.dumps({"env_permissions": {"env-ci": []}}).encode()
        )
        .decode()
        .rstrip("=")
    )
    token = f"header.{payload}.signature"
    monkeypatch.setattr(conftest, "_get_access_token", lambda: token)
    monkeypatch.setattr(
        "chalkcompute._connect.load_token",
        lambda *a, **kw: pytest.fail("SDK must use the scoped token file"),
    )

    with contextmanager(conftest.ci_scoped_auth.__wrapped__)():
        token_path = Path(os.environ["CHALK_WEB_IDENTITY_TOKEN_FILE"])
        assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
        client = ConnectClient()
        try:
            client._ensure_auth()
            assert client._access_token == token
            assert client._env_id == "env-ci"
        finally:
            client.close()
        child_token = subprocess.check_output(
            [
                sys.executable,
                "-c",
                "import os,pathlib; print(pathlib.Path(os.environ['CHALK_WEB_IDENTITY_TOKEN_FILE']).read_text())",
            ],
            text=True,
        ).strip()
        assert child_token == token

    assert "CHALK_WEB_IDENTITY_TOKEN_FILE" not in os.environ
    assert not token_path.exists()


@pytest.mark.parametrize("reason", ["local", "explicit_identity", "no_credentials"])
def test_ci_identity_setup_preserves_other_auth_modes(
    ci_credentials: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reason: str,
) -> None:
    if reason == "local":
        monkeypatch.delenv("BUILDKITE")
    elif reason == "explicit_identity":
        existing = tmp_path / "existing.jwt"
        existing.write_text("existing-token")
        monkeypatch.setenv("CHALK_WEB_IDENTITY_TOKEN_FILE", str(existing))
    else:
        monkeypatch.setattr(conftest, "CHALK_CLIENT_ID", "")
    before = os.environ.get("CHALK_WEB_IDENTITY_TOKEN_FILE")
    monkeypatch.setattr(
        conftest, "_get_access_token", lambda: pytest.fail("unexpected token exchange")
    )

    with contextmanager(conftest.ci_scoped_auth.__wrapped__)():
        assert os.environ.get("CHALK_WEB_IDENTITY_TOKEN_FILE") == before

    assert os.environ.get("CHALK_WEB_IDENTITY_TOKEN_FILE") == before


def test_ci_token_is_removed_when_a_test_fails(
    ci_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conftest, "_get_access_token", lambda: "test-token")

    with pytest.raises(RuntimeError, match="test failed"):
        with contextmanager(conftest.ci_scoped_auth.__wrapped__)():
            token_path = Path(os.environ["CHALK_WEB_IDENTITY_TOKEN_FILE"])
            raise RuntimeError("test failed")

    assert "CHALK_WEB_IDENTITY_TOKEN_FILE" not in os.environ
    assert not token_path.exists()
