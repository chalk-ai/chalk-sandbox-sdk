from __future__ import annotations

from typing import Any

import pytest

from chalkcompute._secret import (
    Secret,
    SecretScope,
    delete_secrets,
    resolve_lazy_secrets,
)


class _Response:
    def __init__(self, status_code: int = 200, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _Client:
    """Fake ConnectClient. `statuses[i]` is the status code for the i-th
    call; calls past the end of the list default to 200."""

    def __init__(self, statuses: list[int] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._statuses = statuses or []

    def rpc(
        self, service: str, method: str, body: dict[str, Any], **kwargs: Any
    ) -> _Response:
        idx = len(self.calls)
        self.calls.append((service, method, body))
        status = self._statuses[idx] if idx < len(self._statuses) else 200
        return _Response(status_code=status, text="boom" if status != 200 else "")


def test_delete_secrets_raises_on_server_rejection() -> None:
    client = _Client(statuses=[200, 500, 200])

    with pytest.raises(RuntimeError, match="secret-b"):
        delete_secrets(client, ["secret-a", "secret-b", "secret-c"])

    # All three deletes were attempted despite the middle one failing.
    assert [c[2]["name"] for c in client.calls] == ["secret-a", "secret-b", "secret-c"]


def test_delete_secrets_passes_scope() -> None:
    client = _Client()
    scope = SecretScope.sandbox("my-container")

    delete_secrets(client, ["secret-a"], scope)

    assert client.calls[0][2]["scope"] == {
        "type": "SECRET_SCOPE_TYPE_SANDBOX",
        "ref": "my-container",
    }


def test_resolve_lazy_secrets_cleans_up_partial_upserts_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRST_VAR", "value-1")
    monkeypatch.delenv("SECOND_VAR", raising=False)

    client = _Client()
    scope = SecretScope.sandbox("my-container")
    secrets = [Secret.from_local_env("FIRST_VAR"), Secret.from_local_env("SECOND_VAR")]

    with pytest.raises(ValueError, match="SECOND_VAR"):
        resolve_lazy_secrets(secrets, client, scope=scope)

    # First secret's upsert succeeded and got a best-effort delete.
    upsert_calls = [c for c in client.calls if c[1] == "UpsertSecret"]
    delete_calls = [c for c in client.calls if c[1] == "DeleteSecret"]
    assert [c[2]["name"] for c in upsert_calls] == ["FIRST_VAR"]
    assert [c[2]["name"] for c in delete_calls] == ["FIRST_VAR"]
    assert delete_calls[0][2]["scope"] == scope._to_proto_dict()


def test_resolve_lazy_secrets_cleanup_failure_does_not_mask_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRST_VAR", "value-1")
    monkeypatch.delenv("SECOND_VAR", raising=False)

    # First UpsertSecret succeeds, then the best-effort DeleteSecret fails.
    client = _Client(statuses=[200, 500])
    secrets = [Secret.from_local_env("FIRST_VAR"), Secret.from_local_env("SECOND_VAR")]

    with pytest.raises(ValueError, match="SECOND_VAR"):
        resolve_lazy_secrets(secrets, client)
