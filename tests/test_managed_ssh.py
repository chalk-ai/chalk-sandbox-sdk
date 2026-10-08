"""Tests for managed SSH destinations and the GitHub preset."""

from __future__ import annotations

from typing import Any

import pytest

import chalkcompute as cc
from chalkcompute._containerspec import (
    GITHUB_SSH_HOST_KEYS,
    ContainerSpec,
    normalize_managed_ssh,
)


def test_github_preset() -> None:
    destination = cc.SshDest.github("GITHUB_KEY")

    assert destination.host == "github.com"
    assert destination.port is None
    assert destination.username == "git"
    assert destination.alias == "github.com"
    assert destination.private_key_secret == "GITHUB_KEY"
    assert destination.host_keys == GITHUB_SSH_HOST_KEYS
    assert {key.split()[0] for key in GITHUB_SSH_HOST_KEYS} == {
        "ssh-ed25519",
        "ecdsa-sha2-nistp256",
        "ssh-rsa",
    }


def test_github_preset_port_443_keeps_github_alias() -> None:
    destination = cc.SshDest.github("GITHUB_KEY", port_443=True)

    assert destination.host == "ssh.github.com"
    assert destination.port == 443
    assert destination.alias == "github.com"
    assert destination.to_dict() == {
        "host": "ssh.github.com",
        "privateKeySecret": "GITHUB_KEY",
        "hostKeys": list(GITHUB_SSH_HOST_KEYS),
        "port": 443,
        "username": "git",
    }


def test_list_keys_destinations_by_alias_then_host() -> None:
    db = cc.SshDest("db.internal", "DB_KEY")
    work = cc.SshDest.github("WORK_KEY", alias="github-work")

    assert normalize_managed_ssh([cc.SshDest.github("KEY"), db, work]) == {
        "github.com": cc.SshDest.github("KEY"),
        "db.internal": db,
        "github-work": work,
    }


def test_mapping_keys_override_alias() -> None:
    destination = cc.SshDest.github("KEY")

    assert normalize_managed_ssh({"gh": destination}) == {"gh": destination}


@pytest.mark.parametrize(
    ("managed_ssh", "error"),
    [
        ([cc.SshDest.github("A"), cc.SshDest.github("B")], ValueError),
        ([cc.SshDest("github.com", "A"), cc.SshDest.github("B")], ValueError),
        ("github.com", TypeError),
        ([{"host": "github.com"}], TypeError),
        ({"": cc.SshDest("h", "K")}, ValueError),
        ({"gh": "github.com"}, TypeError),
    ],
)
def test_normalize_rejects_invalid(managed_ssh: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        normalize_managed_ssh(managed_ssh)


def test_alias_must_not_be_empty() -> None:
    with pytest.raises(ValueError):
        cc.SshDest("github.com", "KEY", alias="")


def test_container_spec_accepts_list() -> None:
    spec = ContainerSpec(
        image="debian:bookworm",
        managed_ssh=[cc.SshDest.github("KEY", port_443=True)],
    )

    assert spec.to_dict()["managedSsh"] == {
        "github.com": cc.SshDest.github("KEY", port_443=True).to_dict()
    }
    proto = spec.to_proto().managed_ssh["github.com"]
    assert (proto.host, proto.port, proto.username) == ("ssh.github.com", 443, "git")


def test_sandbox_accepts_list() -> None:
    sandbox = cc.Sandbox(
        compute_class=cc.ComputeClass.HOST,
        managed_ssh=[cc.SshDest.github("KEY")],
    )

    assert sandbox._managed_ssh == {"github.com": cc.SshDest.github("KEY")}
