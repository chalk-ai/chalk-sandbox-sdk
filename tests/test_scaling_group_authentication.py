from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest

import chalkcompute._scaling_group as scaling_group_mod
from chalkcompute import ScalingGroup
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from tests._fakes import ScalingGroupClient


@pytest.mark.parametrize(
    ("authenticated", "expected"),
    [(False, None), (True, "AUTHENTICATED")],
)
def test_scaling_group_deploy_serializes_authentication(
    authenticated: bool, expected: str | None
) -> None:
    client = ScalingGroupClient()
    sg = ScalingGroup(
        image="nginx:alpine",
        name="auth-test",
        authenticated=authenticated,
    )
    sg._client = cast(Any, client)

    sg.deploy()

    request = client.requests[0]
    assert isinstance(request, sg_pb.CreateScalingGroupRequest)
    assert request.spec is not None and request.spec.container_spec is not None
    assert (request.spec.container_spec.authentication or None) == expected


def test_authenticated_scaling_group_call_adds_chalk_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ScalingGroupClient()
    sg = ScalingGroup(
        image="nginx:alpine",
        name="auth-test",
        authenticated=True,
    )
    sg._client = cast(Any, client)
    sg.deploy()
    original_headers = {"X-Test": "value"}
    http_client = MagicMock()
    monkeypatch.setattr(scaling_group_mod, "SYNC_HTTP_CLIENT", http_client)

    sg.call("/health", method="GET", headers=original_headers)

    assert http_client.execute.call_args.kwargs["headers"] == {
        "X-Test": "value",
        "Authorization": "Bearer test-token",
    }
    assert original_headers == {"X-Test": "value"}
    assert client.ensure_auth_calls == 1


def test_scaling_group_call_preserves_explicit_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ScalingGroupClient()
    sg = ScalingGroup(
        image="nginx:alpine",
        name="auth-test",
        authenticated=True,
    )
    sg._client = cast(Any, client)
    sg.deploy()
    http_client = MagicMock()
    monkeypatch.setattr(scaling_group_mod, "SYNC_HTTP_CLIENT", http_client)

    sg.call(headers={"authorization": "Bearer caller-token"})

    assert http_client.execute.call_args.kwargs["headers"] == {
        "authorization": "Bearer caller-token"
    }
    assert client.ensure_auth_calls == 0


def test_from_name_recovers_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ScalingGroupClient(authentication="AUTHENTICATED")
    monkeypatch.setattr(
        scaling_group_mod,
        "ConnectClient",
        lambda chalk_client=None: client,
    )

    sg = ScalingGroup.from_name("existing-group")

    assert sg._authenticated is True
