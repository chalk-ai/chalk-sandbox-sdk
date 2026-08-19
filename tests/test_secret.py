from __future__ import annotations

import pytest

from chalkcompute._gen.chalk.server.v1 import environment_secrets_pb as secrets_pb
from chalkcompute._secret import Secret, SecretScope, resolve_lazy_secrets
from tests._fakes import FakeMethod, StubbedConnectClient


class _Client(StubbedConnectClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, secrets_pb.UpsertSecretRequest]] = []

    def call(
        self, method: FakeMethod, request: secrets_pb.UpsertSecretRequest, **_kwargs
    ) -> secrets_pb.UpsertSecretResponse:
        self.calls.append((method.name, request))
        return secrets_pb.UpsertSecretResponse()


def test_resolve_lazy_secrets_sends_scope_on_every_upsert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRST_VAR", "value-1")
    monkeypatch.setenv("SECOND_VAR", "value-2")

    client = _Client()
    scope = SecretScope.sandbox("my-container")
    secrets = [Secret.from_local_env("FIRST_VAR"), Secret.from_local_env("SECOND_VAR")]

    resolve_lazy_secrets(secrets, client, scope=scope)

    upsert_calls = [
        request for method, request in client.calls if method == "upsert_secret"
    ]
    assert [request.name for request in upsert_calls] == ["FIRST_VAR", "SECOND_VAR"]
    assert all(request.scope == scope._to_proto() for request in upsert_calls)


def test_resolve_lazy_secrets_without_scope_omits_scope_field() -> None:
    client = _Client()
    secrets = [Secret.from_chalk_env("EXISTING")]

    resolve_lazy_secrets(secrets, client)

    assert client.calls == []


def test_from_env_is_deprecated_alias_for_from_chalk_env() -> None:
    with pytest.warns(DeprecationWarning, match="from_chalk_env"):
        secret = Secret.from_env("MY_SECRET", alias="ALIASED")
    assert secret == Secret.from_chalk_env("MY_SECRET", alias="ALIASED")


def test_from_integration_is_deprecated_alias_for_from_chalk_integration() -> None:
    with pytest.warns(DeprecationWarning, match="from_chalk_integration"):
        secret = Secret.from_integration(
            "prod_postgres", keys=["HOST"], aliases={"HOST": "DB_HOST"}, prefix="PG_"
        )
    assert secret == Secret.from_chalk_integration(
        "prod_postgres", keys=["HOST"], aliases={"HOST": "DB_HOST"}, prefix="PG_"
    )


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
    assert [method for method, _request in client.calls] == ["upsert_secret"]
    assert client.calls[0][1].name == "FIRST_VAR"
