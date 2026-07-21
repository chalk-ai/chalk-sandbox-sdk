from __future__ import annotations

from typing import Any

import pytest

from chalkcompute._secret import Secret, SecretScope, resolve_lazy_secrets


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


def test_resolve_lazy_secrets_sends_scope_on_every_upsert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRST_VAR", "value-1")
    monkeypatch.setenv("SECOND_VAR", "value-2")

    client = _Client()
    scope = SecretScope.sandbox("my-container")
    secrets = [Secret.from_local_env("FIRST_VAR"), Secret.from_local_env("SECOND_VAR")]

    resolve_lazy_secrets(secrets, client, scope=scope)

    upsert_calls = [c for c in client.calls if c[1] == "UpsertSecret"]
    assert [c[2]["name"] for c in upsert_calls] == ["FIRST_VAR", "SECOND_VAR"]
    assert all(c[2]["scope"] == scope._to_proto_dict() for c in upsert_calls)


def test_resolve_lazy_secrets_without_scope_omits_scope_field() -> None:
    client = _Client()
    secrets = [Secret.from_env("EXISTING")]

    resolve_lazy_secrets(secrets, client)

    assert client.calls == []


def test_resolve_lazy_secrets_mid_list_failure_raises_without_deleting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRST_VAR", "value-1")
    monkeypatch.delenv("SECOND_VAR", raising=False)

    client = _Client()
    scope = SecretScope.sandbox("my-container")
    secrets = [Secret.from_local_env("FIRST_VAR"), Secret.from_local_env("SECOND_VAR")]

    with pytest.raises(ValueError, match="SECOND_VAR"):
        resolve_lazy_secrets(secrets, client, scope=scope)

    # The first secret's upsert went through and is left in place; nothing
    # issues a DeleteSecret here.
    assert [c[1] for c in client.calls] == ["UpsertSecret"]
    assert client.calls[0][2]["name"] == "FIRST_VAR"
