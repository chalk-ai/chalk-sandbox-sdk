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


def test_in_memory_secret_uses_normal_scope_and_does_not_export_or_serialize_value(
    monkeypatch,
):
    monkeypatch.delenv("TOKEN", raising=False)
    secret = Secret.from_value("TOKEN", "private-bearer")
    assert secret.is_lazy
    assert "private-bearer" not in repr(secret)
    assert "private-bearer" not in str(secret._to_proto_dict())
    client = _Client()
    scope = SecretScope.sandbox("agent")
    refs, names = resolve_lazy_secrets([secret], client, scope=scope)
    assert refs == [Secret.from_chalk_env("TOKEN")]
    assert names == ["TOKEN"]
    request = client.calls[0][1]
    assert request.scope == scope._to_proto()
    assert request.config.value.value == "private-bearer"
    import os

    assert "TOKEN" not in os.environ


def test_in_memory_secret_works_in_environment_and_host_bound_credentials():
    from chalkcompute import Image, NetworkPolicy, Sandbox
    from chalkcompute._env import normalize_env
    from chalkcompute._secret import prepare_lazy_secrets

    secret = Secret.from_value("TOKEN", "private-bearer")
    literals, refs = normalize_env({"ALIASED": secret})
    assert literals == {}
    resolved, values = prepare_lazy_secrets(refs)
    assert values == {"TOKEN": "private-bearer"}
    assert resolved == [Secret.from_chalk_env("TOKEN", alias="ALIASED")]
    sandbox = Sandbox(
        image=Image.base("python:3.12"), network_policy=NetworkPolicy(allowed_hosts=[])
    )
    sandbox.add_credentials("api.example.com", secret)
    assert sandbox._env["TOKEN"].startswith("__chalk-secret-")
    assert "private-bearer" not in repr(sandbox._network_policy)


@pytest.mark.parametrize(
    "name,value,error", [("", "value", ValueError), ("TOKEN", 123, TypeError)]
)
def test_in_memory_secret_validates_without_exposing_value(name, value, error):
    with pytest.raises(error):
        Secret.from_value(name, value)


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
