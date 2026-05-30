"""Tests for ambient Chalk credential injection into containers/functions."""

from __future__ import annotations

import logging
from typing import Any

import pytest

import chalkcompute._auth_config as _auth_config
from chalkcompute._auth_config import AuthConfig, TokenConfig
from chalkcompute._secret import Secret, resolve_ambient_chalk_secrets

# Tests in this file exercise `_load_global_config`'s real behavior, so they
# override the conftest's autouse patch that stubs it to `lambda: None`.
# Each test uses `monkeypatch.setattr(_auth_config, "_load_global_config", ...)`
# to install its own stub when it wants to control the file-based path.


class _FakeClient:
    """Captures the upsert RPCs made by `resolve_ambient_chalk_secrets`."""

    def __init__(self) -> None:
        self.upserts: list[tuple[str, str]] = []

    def rpc(self, service: str, method: str, payload: dict[str, Any], **_kw: Any) -> Any:
        assert method == "UpsertSecret"
        name = payload["name"]
        value = payload["config"]["literal"]
        self.upserts.append((name, value))

        class _Resp:
            status_code = 200
            text = ""

        return _Resp()


def _clear_chalk_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "CHALK_CLIENT_ID",
        "CHALK_CLIENT_SECRET",
        "CHALK_API_SERVER",
        "CHALK_ENVIRONMENT",
        "CHALK_ENVIRONMENT_ID",
        "CHALK_ACTIVE_ENVIRONMENT",
        "_CHALK_CLIENT_ID",
        "_CHALK_CLIENT_SECRET",
        "_CHALK_ACTIVE_ENVIRONMENT",
    ):
        monkeypatch.delenv(name, raising=False)


class TestResolveAmbientChalkSecrets:
    def test_injects_from_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_chalk_env(monkeypatch)
        monkeypatch.setenv("CHALK_CLIENT_ID", "cid-env")
        monkeypatch.setenv("CHALK_CLIENT_SECRET", "csec-env")
        monkeypatch.setenv("CHALK_API_SERVER", "https://api.example.com")
        monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "env-123")

        client = _FakeClient()
        ambient, upserted = resolve_ambient_chalk_secrets([], None, client)

        names = {s._secret_name for s in ambient}
        assert names == {
            "CHALK_CLIENT_ID",
            "CHALK_CLIENT_SECRET",
            "CHALK_API_SERVER",
            "CHALK_ENVIRONMENT_ID",
        }
        assert set(upserted) == names
        assert dict(client.upserts) == {
            "CHALK_CLIENT_ID": "cid-env",
            "CHALK_CLIENT_SECRET": "csec-env",
            "CHALK_API_SERVER": "https://api.example.com",
            "CHALK_ENVIRONMENT_ID": "env-123",
        }

    def test_injects_from_chalk_yml_without_env_vars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_chalk_env(monkeypatch)
        monkeypatch.setattr(
            _auth_config,
            "_load_global_config",
            lambda: AuthConfig(
                tokens={
                    "default": TokenConfig(
                        clientId="cid-yml",
                        clientSecret="csec-yml",
                        apiServer="https://api.yml.chalk.ai",
                        activeEnvironment="env-yml",
                        name="default",
                    )
                }
            ),
        )
        monkeypatch.setattr(_auth_config, "load_project_config", lambda: None)

        client = _FakeClient()
        ambient, upserted = resolve_ambient_chalk_secrets([], None, client)

        assert {s._secret_name for s in ambient} == {
            "CHALK_CLIENT_ID",
            "CHALK_CLIENT_SECRET",
            "CHALK_API_SERVER",
            "CHALK_ENVIRONMENT_ID",
        }
        assert dict(client.upserts) == {
            "CHALK_CLIENT_ID": "cid-yml",
            "CHALK_CLIENT_SECRET": "csec-yml",
            "CHALK_API_SERVER": "https://api.yml.chalk.ai",
            "CHALK_ENVIRONMENT_ID": "env-yml",
        }
        assert set(upserted) == {
            "CHALK_CLIENT_ID",
            "CHALK_CLIENT_SECRET",
            "CHALK_API_SERVER",
            "CHALK_ENVIRONMENT_ID",
        }

    def test_omits_environment_when_token_has_no_active_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_chalk_env(monkeypatch)
        monkeypatch.setenv("CHALK_CLIENT_ID", "cid")
        monkeypatch.setenv("CHALK_CLIENT_SECRET", "csec")
        monkeypatch.setenv("CHALK_API_SERVER", "https://api.example.com")

        client = _FakeClient()
        ambient, upserted = resolve_ambient_chalk_secrets([], None, client)

        names = {s._secret_name for s in ambient}
        assert "CHALK_ENVIRONMENT_ID" not in names
        assert names == {
            "CHALK_CLIENT_ID",
            "CHALK_CLIENT_SECRET",
            "CHALK_API_SERVER",
        }
        assert "CHALK_ENVIRONMENT_ID" not in dict(client.upserts)
        assert "CHALK_ENVIRONMENT_ID" not in upserted

    def test_skips_name_already_declared_as_secret_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_chalk_env(monkeypatch)
        monkeypatch.setenv("CHALK_CLIENT_ID", "cid")
        monkeypatch.setenv("CHALK_CLIENT_SECRET", "csec")
        monkeypatch.setenv("CHALK_API_SERVER", "https://api.example.com")

        existing = [Secret.from_env("CHALK_CLIENT_ID")]
        client = _FakeClient()
        ambient, upserted = resolve_ambient_chalk_secrets(existing, None, client)

        injected = {s._secret_name for s in ambient}
        assert "CHALK_CLIENT_ID" not in injected
        assert "CHALK_CLIENT_ID" not in dict(client.upserts)
        assert "CHALK_CLIENT_ID" not in upserted

    def test_skips_name_already_declared_via_alias(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_chalk_env(monkeypatch)
        monkeypatch.setenv("CHALK_CLIENT_ID", "cid")
        monkeypatch.setenv("CHALK_CLIENT_SECRET", "csec")
        monkeypatch.setenv("CHALK_API_SERVER", "https://api.example.com")

        existing = [Secret.from_env("MY_BOT_ID", alias="CHALK_CLIENT_ID")]
        client = _FakeClient()
        ambient, _ = resolve_ambient_chalk_secrets(existing, None, client)

        injected = {s._secret_name for s in ambient}
        assert "CHALK_CLIENT_ID" not in injected
        assert "CHALK_CLIENT_ID" not in dict(client.upserts)

    def test_skips_name_already_declared_via_local_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_chalk_env(monkeypatch)
        monkeypatch.setenv("CHALK_CLIENT_ID", "cid")
        monkeypatch.setenv("CHALK_CLIENT_SECRET", "csec")
        monkeypatch.setenv("CHALK_API_SERVER", "https://api.example.com")

        existing = [Secret.from_local_env("CHALK_CLIENT_ID")]
        client = _FakeClient()
        ambient, _ = resolve_ambient_chalk_secrets(existing, None, client)

        injected = {s._secret_name for s in ambient}
        assert "CHALK_CLIENT_ID" not in injected
        assert "CHALK_CLIENT_ID" not in dict(client.upserts)

    def test_skips_name_already_declared_via_local_env_with_rename(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_chalk_env(monkeypatch)
        monkeypatch.setenv("CHALK_CLIENT_ID", "cid")
        monkeypatch.setenv("CHALK_CLIENT_SECRET", "csec")
        monkeypatch.setenv("CHALK_API_SERVER", "https://api.example.com")

        existing = [
            Secret.from_local_env("MY_BOT_ID", env_var_name="CHALK_CLIENT_ID"),
        ]
        client = _FakeClient()
        ambient, _ = resolve_ambient_chalk_secrets(existing, None, client)

        injected = {s._secret_name for s in ambient}
        assert "CHALK_CLIENT_ID" not in injected
        assert "CHALK_CLIENT_ID" not in dict(client.upserts)

    def test_skips_name_already_in_env_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_chalk_env(monkeypatch)
        monkeypatch.setenv("CHALK_CLIENT_ID", "cid")
        monkeypatch.setenv("CHALK_CLIENT_SECRET", "csec")
        monkeypatch.setenv("CHALK_API_SERVER", "https://api.example.com")

        env = {"CHALK_CLIENT_ID": "from-env-dict"}
        client = _FakeClient()
        ambient, _ = resolve_ambient_chalk_secrets([], env, client)

        injected = {s._secret_name for s in ambient}
        assert "CHALK_CLIENT_ID" not in injected
        assert "CHALK_CLIENT_ID" not in dict(client.upserts)

    def test_no_credentials_returns_empty_and_warns(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _clear_chalk_env(monkeypatch)
        monkeypatch.setattr(_auth_config, "_load_global_config", lambda: None)
        monkeypatch.setattr(_auth_config, "load_project_config", lambda: None)

        client = _FakeClient()
        with caplog.at_level(logging.WARNING):
            ambient, upserted = resolve_ambient_chalk_secrets([], None, client)

        assert ambient == []
        assert upserted == []
        assert client.upserts == []
        assert any(
            "ambient Chalk credentials" in r.message.lower()
            or "chalk credentials" in r.message.lower()
            for r in caplog.records
        )
