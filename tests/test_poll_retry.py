"""Scaling-group deploy/status polls must survive transient control-plane blips.

A single envoy "connection refused" mid-poll must not kill the client while
the server-side deploy continues fine; only ``POLL_MAX_ATTEMPTS`` consecutive
failures raise.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from chalkcompute._connect import POLL_MAX_ATTEMPTS, ConnectError, rpc_with_retries
from chalkcompute._scaling_group import ScalingGroup, ScalingGroupError

_SVC = "chalk.scalinggroup.v1.ScalingGroupManagerService"


class _Response:
    def __init__(
        self,
        data: dict[str, Any] | None = None,
        status_code: int = 200,
        text: str = "",
    ) -> None:
        self._data = data or {}
        self.status_code = status_code
        self.text = text

    def json(self) -> dict[str, Any]:
        return self._data


class _FlakyClient:
    """Pops one scripted outcome per rpc call: an exception to raise or a
    response to return."""

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def rpc(self, _service: str, _method: str, _body: dict, **_: Any) -> _Response:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


_BLIP = "upstream connect error or disconnect/reset before headers: connection refused"


def test_rpc_with_retries_recovers_from_transient_failures() -> None:
    ok = _Response({"scalingGroup": {}})
    client = _FlakyClient(
        [
            httpx.ConnectError("connection refused"),
            _Response(status_code=503, text=_BLIP),
            ok,
        ]
    )
    resp = rpc_with_retries(
        client,  # type: ignore[arg-type]
        _SVC,
        "GetScalingGroup",
        {"id": "sg-1"},
        retry_interval=0,
    )
    assert resp is ok
    assert client.calls == 3


def test_rpc_with_retries_raises_error_cls_after_all_attempts_fail() -> None:
    client = _FlakyClient(
        [httpx.ConnectError("connection refused") for _ in range(POLL_MAX_ATTEMPTS)]
    )
    with pytest.raises(ScalingGroupError, match="after 5 attempts"):
        rpc_with_retries(
            client,  # type: ignore[arg-type]
            _SVC,
            "GetScalingGroup",
            {"id": "sg-1"},
            error_cls=ScalingGroupError,
            retry_interval=0,
        )
    assert client.calls == POLL_MAX_ATTEMPTS


def test_rpc_with_retries_defaults_to_connect_error() -> None:
    client = _FlakyClient(
        [_Response(status_code=503, text=_BLIP) for _ in range(POLL_MAX_ATTEMPTS)]
    )
    with pytest.raises(ConnectError, match="HTTP 503"):
        rpc_with_retries(
            client,  # type: ignore[arg-type]
            _SVC,
            "GetScalingGroup",
            {"id": "sg-1"},
            retry_interval=0,
        )


@pytest.mark.parametrize("status_code", [400, 404])
def test_rpc_with_retries_raises_immediately_on_client_errors(
    status_code: int,
) -> None:
    client = _FlakyClient([_Response(status_code=status_code, text="nope")])
    with pytest.raises(ConnectError, match=f"HTTP {status_code}"):
        rpc_with_retries(
            client,  # type: ignore[arg-type]
            _SVC,
            "GetScalingGroup",
            {"id": "sg-1"},
            retry_interval=0,
        )
    assert client.calls == 1


def test_rpc_with_retries_retries_429() -> None:
    ok = _Response({"scalingGroup": {}})
    client = _FlakyClient([_Response(status_code=429, text="rate limited"), ok])
    resp = rpc_with_retries(
        client,  # type: ignore[arg-type]
        _SVC,
        "GetScalingGroup",
        {"id": "sg-1"},
        retry_interval=0,
    )
    assert resp is ok
    assert client.calls == 2


def _sg_response(**overrides: Any) -> _Response:
    sg = {
        "id": "sg-1",
        "name": "sg-retry",
        "status": "Running",
        "webUrl": "http://127.0.0.1",
        "readyReplicas": 0,
        "availableReplicas": 0,
        **overrides,
    }
    return _Response({"scalingGroup": sg})


def test_scaling_group_poll_survives_blip() -> None:
    client = _FlakyClient(
        [
            httpx.ConnectError("connection refused"),
            _sg_response(),
            _Response(status_code=503, text=_BLIP),
            _sg_response(readyReplicas=1, availableReplicas=1),
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
        [httpx.ConnectError("connection refused") for _ in range(POLL_MAX_ATTEMPTS)]
    )
    sg = ScalingGroup(image="ubuntu:latest", name="sg-retry")
    sg._client = client  # type: ignore[assignment]
    sg._scaling_group_id = "sg-1"

    with pytest.raises(ScalingGroupError, match="after 5 attempts"):
        sg._poll_until_ready(poll_interval=0, timeout=30)
    assert client.calls == POLL_MAX_ATTEMPTS
