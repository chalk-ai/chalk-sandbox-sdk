from __future__ import annotations

import pytest

import chalkcompute as cc
from chalkcompute._env import normalize_env
from chalkcompute._secret import prepare_lazy_secrets
from chalkcompute._training import training


def test_normalize_env_splits_literals_and_secret_references_without_mutation():
    secret = cc.Secret.from_chalk_env("TOKEN", alias="OLD", prefix="PREFIX_")
    env = {"NAME": "Elliot", "API_KEY": secret}
    explicit = [cc.Secret.from_chalk_env("OTHER")]

    assert normalize_env(env, explicit) == (
        {"NAME": "Elliot"},
        [explicit[0], cc.Secret.from_chalk_env("TOKEN", alias="API_KEY")],
    )
    assert env["API_KEY"] is secret
    assert explicit == [cc.Secret.from_chalk_env("OTHER")]


def test_normalize_env_accepts_one_integration_key():
    assert normalize_env(
        {
            "PASSWORD": cc.Secret.from_chalk_integration(
                "postgres", keys=["TOKEN"], aliases={"TOKEN": "OLD"}, prefix="P_"
            )
        }
    ) == (
        {},
        [
            cc.Secret.from_chalk_integration(
                "postgres", keys=["TOKEN"], aliases={"TOKEN": "PASSWORD"}
            )
        ],
    )


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
    assert secret == cc.Secret.from_local_env("LOCAL_TOKEN", env_var_name="OLD")


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
    factory(
        env={"NAME": "Elliot", "API_KEY": cc.Secret.from_chalk_env("TOKEN")},
        secrets=[cc.Secret.from_chalk_env("OTHER")],
    )


def test_training_rejects_local_secret():
    with pytest.raises(ValueError, match="Training does not support local secrets"):
        training(data="dataset", env={"API_KEY": cc.Secret.from_local_env("TOKEN")})(
            _function
        )
