from __future__ import annotations

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
    monkeypatch.delenv("CHALK_GRPC_ENGINE", raising=False)
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
