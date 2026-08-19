"""Scaling-group deploy/status polls must survive transient control-plane blips.

A single envoy "connection refused" mid-poll must not kill the client while
the server-side deploy continues fine; only ``POLL_MAX_ATTEMPTS`` consecutive
failures raise.
"""

from __future__ import annotations

from typing import Any

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError

from chalkcompute._connect import POLL_MAX_ATTEMPTS, ConnectError, rpc_with_retries
from chalkcompute._gen.chalk.scalinggroup.v1 import service_connect as sg_connect
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from chalkcompute._scaling_group import ScalingGroup, ScalingGroupError
from tests._fakes import FakeMethod, StubbedConnectClient


class _FlakyClient(StubbedConnectClient):
    """Pops one scripted outcome per generated call: an exception to raise or a
    response to return."""

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def call(self, _method: FakeMethod, _request: Any, **_: Any) -> Any:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


_BLIP = "upstream connect error or disconnect/reset before headers: connection refused"


def _get_with_retries(
    client: _FlakyClient, *, error_cls: type[Exception] = ConnectError
) -> sg_pb.GetScalingGroupResponse:
    stub = client.stub(sg_connect.ScalingGroupManagerServiceClientSync)
    return rpc_with_retries(
        client,  # type: ignore[arg-type]
        stub.get_scaling_group,
        sg_pb.GetScalingGroupRequest(id="sg-1"),
        error_cls=error_cls,
        retry_interval=0,
    )


def test_rpc_with_retries_recovers_from_transient_failures() -> None:
    ok = sg_pb.GetScalingGroupResponse(scaling_group=sg_pb.ScalingGroupResponse())
    client = _FlakyClient(
        [
            ConnectRpcError(Code.UNAVAILABLE, "connection refused"),
            ConnectRpcError(Code.UNAVAILABLE, _BLIP),
            ok,
        ]
    )
    resp = _get_with_retries(client)
    assert resp.scaling_group is not None
    assert client.calls == 3


def test_rpc_with_retries_raises_error_cls_after_all_attempts_fail() -> None:
    client = _FlakyClient(
        [
            ConnectRpcError(Code.UNAVAILABLE, "connection refused")
            for _ in range(POLL_MAX_ATTEMPTS)
        ]
    )
    with pytest.raises(ScalingGroupError, match="after 5 attempts"):
        _get_with_retries(client, error_cls=ScalingGroupError)
    assert client.calls == POLL_MAX_ATTEMPTS


def test_rpc_with_retries_defaults_to_connect_error() -> None:
    client = _FlakyClient(
        [ConnectRpcError(Code.UNAVAILABLE, _BLIP) for _ in range(POLL_MAX_ATTEMPTS)]
    )
    with pytest.raises(ConnectError, match="after 5 attempts"):
        _get_with_retries(client)


@pytest.mark.parametrize("code", [Code.INVALID_ARGUMENT, Code.NOT_FOUND])
def test_rpc_with_retries_raises_immediately_on_client_errors(
    code: Code,
) -> None:
    client = _FlakyClient([ConnectRpcError(code, "nope")])
    with pytest.raises(ConnectError):
        _get_with_retries(client)
    assert client.calls == 1


def test_rpc_with_retries_retries_429() -> None:
    ok = sg_pb.GetScalingGroupResponse(scaling_group=sg_pb.ScalingGroupResponse())
    client = _FlakyClient(
        [ConnectRpcError(Code.RESOURCE_EXHAUSTED, "rate limited"), ok]
    )
    resp = _get_with_retries(client)
    assert resp.scaling_group is not None
    assert client.calls == 2


def _sg_response(**overrides: Any) -> sg_pb.GetScalingGroupResponse:
    return sg_pb.GetScalingGroupResponse(
        scaling_group=sg_pb.ScalingGroupResponse(
            id="sg-1",
            name="sg-retry",
            status="Running",
            web_url="http://127.0.0.1",
            ready_replicas=overrides.get("ready_replicas", 0),
            available_replicas=overrides.get("available_replicas", 0),
        )
    )


def test_scaling_group_poll_survives_blip() -> None:
    client = _FlakyClient(
        [
            ConnectRpcError(Code.UNAVAILABLE, "connection refused"),
            _sg_response(),
            ConnectRpcError(Code.UNAVAILABLE, _BLIP),
            _sg_response(ready_replicas=1, available_replicas=1),
        ]
    )
    sg = ScalingGroup(image="ubuntu:latest", name="sg-retry")
    sg._client = client  # type: ignore[assignment]
    sg._scaling_group_id = "sg-1"

    sg._poll_until_ready(poll_interval=0, timeout=30)

    assert client.calls == 4
    assert sg.info is not None and sg.info.ready_replicas == 1


def test_scaling_group_poll_raises_after_all_attempts_fail() -> None:
    client = _FlakyClient(
        [
            ConnectRpcError(Code.UNAVAILABLE, "connection refused")
            for _ in range(POLL_MAX_ATTEMPTS)
        ]
    )
    sg = ScalingGroup(image="ubuntu:latest", name="sg-retry")
    sg._client = client  # type: ignore[assignment]
    sg._scaling_group_id = "sg-1"

    with pytest.raises(ScalingGroupError, match="after 5 attempts"):
        sg._poll_until_ready(poll_interval=0, timeout=30)
    assert client.calls == POLL_MAX_ATTEMPTS
