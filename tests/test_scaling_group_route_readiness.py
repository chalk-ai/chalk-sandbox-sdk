from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest

import chalkcompute._scaling_group as scaling_group_mod
from chalkcompute import ReadinessProbe, ScalingGroup, StartupProbe
from tests._fakes import ScalingGroupClient


class Clock:
    """Advance time without sleeping so the full retry deadline is tested."""

    def __init__(self) -> None:
        self.now = 0.0
        self.delays: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.now += delay


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(scaling_group_mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(scaling_group_mod.time, "sleep", clock.sleep)
    return clock


@pytest.fixture
def http_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock()
    monkeypatch.setattr(scaling_group_mod, "SYNC_HTTP_CLIENT", client)
    return client


def response(status: int, body: bytes) -> MagicMock:
    return MagicMock(status=status, content=body)


@pytest.mark.parametrize("authenticated", [False, True])
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, b"ok"),
        (404, b"no such path"),
        (401, b""),
        (500, b"error"),
        (503, b"maintenance"),
    ],
)
def test_deploy_waits_for_route_then_allows_first_call(
    clock: Clock, http_client: MagicMock, authenticated: bool, status: int, body: bytes
) -> None:
    """Application responses establish routing even if the root path fails."""
    http_client.execute.side_effect = [
        response(404, b""),
        TimeoutError("connection timed out"),
        response(503, b"upstream connect error"),
        response(status, body),
        response(200, b"first real call"),
    ]
    group = ScalingGroup(image="nginx:alpine", authenticated=authenticated)
    group._client = cast(Any, ScalingGroupClient(web_url="https://example.com"))

    assert group.deploy() is group
    assert http_client.execute.call_count == 4
    assert clock.delays == [0.25, 0.5, 1.0]
    for call in http_client.execute.call_args_list:
        assert call.args == ("GET", "https://example.com/")
        assert call.kwargs["headers"] == (
            {"Authorization": "Bearer test-token"} if authenticated else None
        )
    assert group.call("/work", method="GET", cold_start_timeout=0).content == (
        b"first real call"
    )


@pytest.mark.parametrize(
    "result",
    [
        response(404, b""),
        response(503, b"no healthy upstream"),
        ConnectionError("offline"),
    ],
)
@pytest.mark.parametrize(
    "readiness_probe", [None, ReadinessProbe.http("/health", timeout_seconds=5)]
)
def test_route_wait_warns_and_returns_at_deadline(
    clock: Clock,
    http_client: MagicMock,
    caplog: pytest.LogCaptureFixture,
    result: MagicMock | Exception,
    readiness_probe: ReadinessProbe | None,
) -> None:
    requests: list[tuple[float, float]] = []

    def request(*args: Any, timeout: float, **kwargs: Any) -> MagicMock:
        # A slow request must fit within the remaining overall deadline too.
        requests.append((clock.now, timeout))
        clock.now += timeout
        if isinstance(result, Exception):
            raise result
        return result

    http_client.execute.side_effect = request
    group = ScalingGroup(
        image="nginx:alpine", name="route-timeout", readiness_probe=readiness_probe
    )
    group._client = cast(Any, ScalingGroupClient(web_url="https://example.com"))

    assert group.deploy() is group

    assert all(0 < timeout <= min(5, 60 - started) for started, timeout in requests)
    assert clock.now == 60
    assert max(clock.delays) <= 2
    assert "route-timeout route was not ready after 60.0s" in caplog.text
    expected_status = (
        type(result).__name__ if isinstance(result, Exception) else str(result.status)
    )
    assert f"last status={expected_status}" in caplog.text


@pytest.mark.parametrize(
    ("options", "web_url"),
    [
        ({"min_replicas": 0}, "https://example.com"),
        ({"protocol": "grpc"}, "https://example.com"),
        ({"port": None}, None),
        ({}, None),
        ({"startup_probe": StartupProbe.http("/health")}, "https://example.com"),
    ],
)
def test_deploy_skips_unnecessary_route_wait(
    http_client: MagicMock, options: dict[str, Any], web_url: str | None
) -> None:
    group = ScalingGroup(image="nginx:alpine", **options)
    group._client = cast(Any, ScalingGroupClient(web_url=web_url))

    group.deploy()

    http_client.execute.assert_not_called()
