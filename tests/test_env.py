from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import chalkcompute as cc
from chalkcompute._env import normalize_env
from chalkcompute._secret import prepare_lazy_secrets
from chalkcompute._training import training


@pytest.mark.parametrize(
    "secret",
    [
        cc.Secret.from_chalk_env("TOKEN", alias="OLD", prefix="PREFIX_"),
        cc.Secret.from_chalk_integration(
            "postgres", keys=["TOKEN"], aliases={"TOKEN": "OLD"}, prefix="PREFIX_"
        ),
    ],
)
def test_env_key_overrides_secret_alias_and_prefix_without_mutation(secret):
    original = secret._to_proto_dict()
    env = {"NAME": "Elliot", "API_KEY": secret, "SECOND_KEY": secret}
    explicit = [cc.Secret.from_chalk_env("OTHER")]
    literals, refs = normalize_env(env, explicit)

    assert literals == {"NAME": "Elliot"}
    assert refs[0] == explicit[0]
    assert refs[1]._to_proto_dict() == {
        **{k: v for k, v in original.items() if k not in {"aliases", "prefix"}},
        "aliases": {"TOKEN": "API_KEY"},
    }
    assert refs[2]._aliases == {"TOKEN": "SECOND_KEY"}
    assert secret._to_proto_dict() == original
    assert env["API_KEY"] is secret
    assert len(explicit) == 1


def test_env_local_secret_stays_lazy_and_resolves_under_env_key(monkeypatch):
    monkeypatch.delenv("LOCAL_TOKEN", raising=False)
    secret = cc.Secret.from_local_env("LOCAL_TOKEN", env_var_name="OLD")
    literals, refs = normalize_env({"API_KEY": secret})
    assert literals == {}
    assert refs[0].is_lazy
    monkeypatch.setenv("LOCAL_TOKEN", "private-value")
    resolved, values = prepare_lazy_secrets(refs)
    assert values == {"API_KEY": "private-value"}
    assert resolved == [cc.Secret.from_chalk_env("API_KEY")]
    assert secret._env_var_name == "OLD"


@pytest.mark.parametrize(
    "secret",
    [
        cc.Secret.from_chalk_integration("postgres"),
        cc.Secret.from_chalk_integration("postgres", keys=["HOST", "PASSWORD"]),
        cc.Secret.from_local_env_file("nonexistent.env"),
    ],
)
def test_bulk_secrets_require_secrets_list(secret):
    with pytest.raises(ValueError, match="secrets=\\[.*\\]"):
        normalize_env({"API_KEY": secret})
    assert normalize_env(None, [secret]) == ({}, [secret])


@pytest.mark.parametrize(
    "env, error, match",
    [
        ({"TOKEN": None}, TypeError, "os.getenv"),
        ({"PORT": 123}, TypeError, "PORT"),
        ({"": cc.Secret.from_chalk_env("TOKEN")}, ValueError, "non-empty"),
    ],
)
def test_env_validation_preserved(env, error, match):
    with pytest.raises(error, match=match):
        normalize_env(env)


def _function():
    return "ok"


class _Class:
    @cc.method
    def run(self):
        return "ok"


@pytest.mark.parametrize(
    "factory",
    [
        lambda **kwargs: cc.Sandbox(image="ubuntu:latest", **kwargs),
        lambda **kwargs: cc.Container(image="ubuntu:latest", **kwargs),
        lambda **kwargs: cc.ScalingGroup(image="ubuntu:latest", port=8080, **kwargs),
        lambda **kwargs: cc.function(**kwargs)(_function),
        lambda **kwargs: cc.cls(**kwargs)(_Class),
        lambda **kwargs: training(data="dataset", **kwargs)(_function),
    ],
    ids=["sandbox", "container", "scaling-group", "function", "class", "training"],
)
def test_all_deployment_entrypoints_accept_mixed_env(factory, monkeypatch):
    monkeypatch.setattr(
        cc.RemoteFunction, "_start_background_deploy", lambda self: None
    )
    monkeypatch.setattr(cc.RemoteClass, "deploy", lambda self: self)
    consumer = factory(
        env={"NAME": "Elliot", "API_KEY": cc.Secret.from_chalk_env("TOKEN")},
        secrets=[cc.Secret.from_chalk_env("OTHER")],
    )
    assert consumer._env == {"NAME": "Elliot"}
    assert consumer._secrets == [
        cc.Secret.from_chalk_env("OTHER"),
        cc.Secret.from_chalk_env("TOKEN", alias="API_KEY"),
    ]


@pytest.mark.parametrize(
    "method", ["exec", "exec_stream", "exec_start", "execute_python"]
)
def test_per_exec_env_rejects_secret_with_actionable_error(method):
    sandbox = cc.Sandbox(sandbox_id="sandbox-123", client=MagicMock())
    with pytest.raises(TypeError, match="when creating a sandbox"):
        result = getattr(sandbox, method)(
            "echo", env={"API_KEY": cc.Secret.from_chalk_env("TOKEN")}
        )
        if method == "exec_stream":
            list(result)


def test_image_env_rejects_secret_with_actionable_error():
    with pytest.raises(TypeError, match="Image and per-exec env require strings"):
        cc.Image.debian_slim().env({"API_KEY": cc.Secret.from_chalk_env("TOKEN")})


def test_training_rejects_local_secret_with_actionable_error():
    with pytest.raises(ValueError, match="Training does not support local secrets"):
        training(data="dataset", env={"API_KEY": cc.Secret.from_local_env("TOKEN")})(
            _function
        )
