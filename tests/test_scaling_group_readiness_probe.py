from __future__ import annotations

from typing import Any, cast

import pytest

from chalkcompute import ReadinessProbe, ScalingGroup


class _Response:
    status_code = 200
    text = ""

    def json(self) -> dict[str, Any]:
        return {
            "scalingGroup": {
                "id": "sg-1",
                "name": "sg-readiness",
                "status": "Running",
                "readyReplicas": 1,
                "availableReplicas": 1,
            }
        }


class _CapturingClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def rpc(
        self,
        _service: str,
        method: str,
        request: dict[str, Any],
        **_: Any,
    ) -> _Response:
        assert method == "CreateScalingGroup"
        self.requests.append(request)
        return _Response()


@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        (
            ReadinessProbe.http(
                "/readyz",
                period_seconds=5,
                timeout_seconds=2,
                failure_threshold=4,
            ),
            {
                "http": {"path": "/readyz"},
                "periodSeconds": 5,
                "timeoutSeconds": 2,
                "failureThreshold": 4,
            },
        ),
        (
            ReadinessProbe.grpc(service="chalk.compute.v1.Worker"),
            {"grpc": {"service": "chalk.compute.v1.Worker"}},
        ),
        (ReadinessProbe.http(), {"http": {}}),
        (ReadinessProbe.grpc(), {"grpc": {}}),
    ],
)
def test_readiness_probe_to_dict(
    probe: ReadinessProbe, expected: dict[str, Any]
) -> None:
    assert probe.to_dict() == expected


def test_scaling_group_deploy_includes_readiness_probe() -> None:
    client = _CapturingClient()
    sg = ScalingGroup(
        image="ubuntu:latest",
        name="sg-readiness",
        port=8080,
        readiness_probe=ReadinessProbe.http("/readyz"),
    )
    sg._client = cast(Any, client)

    sg.deploy()

    assert client.requests[0]["spec"]["containerSpec"]["readinessProbe"] == {
        "http": {"path": "/readyz"}
    }


def test_scaling_group_deploy_omits_readiness_probe_by_default() -> None:
    client = _CapturingClient()
    sg = ScalingGroup(
        image="ubuntu:latest",
        name="sg-readiness",
        port=8080,
    )
    sg._client = cast(Any, client)

    sg.deploy()

    assert "readinessProbe" not in client.requests[0]["spec"]["containerSpec"]
