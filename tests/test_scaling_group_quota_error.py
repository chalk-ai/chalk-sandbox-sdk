from __future__ import annotations

from typing import Any, cast

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError

import chalkcompute
from chalkcompute import QuotaExceededError, ScalingGroup, ScalingGroupError
from tests._fakes import FakeMethod, StubbedConnectClient

_QUOTA_MESSAGE = (
    "CPU quota exceeded: requested 32 cores, but only 16 of the 16 cores limit "
    "is available (0 in use). To raise this limit, open Settings > Quotas in the "
    'Chalk dashboard and click "Request increase".'
)


class _RejectingClient(StubbedConnectClient):
    def __init__(self, error: ConnectError) -> None:
        self.error = error
        self.methods: list[str] = []

    def call(self, method: FakeMethod, request: Any, **_: Any) -> Any:
        self.methods.append(method.name)
        raise self.error


def _deploy(error: ConnectError, *, scaling_group_id: str | None = None) -> None:
    sg = ScalingGroup(image="ubuntu:latest", name="sg-quota", port=8080)
    sg._scaling_group_id = scaling_group_id
    client = _RejectingClient(error)
    sg._client = cast(Any, client)
    sg.deploy()


def test_create_quota_rejection_raises_quota_exceeded_error() -> None:
    error = ConnectError(Code.RESOURCE_EXHAUSTED, _QUOTA_MESSAGE)

    with pytest.raises(QuotaExceededError) as exc_info:
        _deploy(error)

    assert str(exc_info.value) == _QUOTA_MESSAGE
    assert exc_info.value.__cause__ is error


def test_update_quota_rejection_raises_quota_exceeded_error() -> None:
    error = ConnectError(Code.RESOURCE_EXHAUSTED, _QUOTA_MESSAGE)

    with pytest.raises(QuotaExceededError) as exc_info:
        _deploy(error, scaling_group_id="sg-1")

    assert str(exc_info.value) == _QUOTA_MESSAGE


def test_quota_exceeded_error_is_caught_as_scaling_group_error() -> None:
    error = ConnectError(Code.RESOURCE_EXHAUSTED, _QUOTA_MESSAGE)

    with pytest.raises(ScalingGroupError):
        _deploy(error)


def test_other_rejections_keep_scaling_group_error() -> None:
    error = ConnectError(Code.INVALID_ARGUMENT, "bad port")

    with pytest.raises(ScalingGroupError) as exc_info:
        _deploy(error)

    assert not isinstance(exc_info.value, QuotaExceededError)
    assert str(exc_info.value) == "CreateScalingGroup failed: bad port"


def test_quota_exceeded_error_is_exported() -> None:
    assert "QuotaExceededError" in chalkcompute.__all__
    assert issubclass(chalkcompute.QuotaExceededError, chalkcompute.ScalingGroupError)
