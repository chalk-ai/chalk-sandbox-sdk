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


def _deployed_container_spec(sg: ScalingGroup) -> dict[str, Any]:
    client = _CapturingClient()
    sg._client = cast(Any, client)
    sg.deploy()
    return client.requests[0]["spec"]["containerSpec"]


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
    sg = ScalingGroup(
        image="ubuntu:latest",
        name="sg-readiness",
        port=8080,
        readiness_probe=ReadinessProbe.http("/readyz"),
    )
    container_spec = _deployed_container_spec(sg)

    assert container_spec["readinessProbe"] == {"http": {"path": "/readyz"}}
    assert container_spec["port"] == 8080


def test_scaling_group_deploy_omits_readiness_probe_by_default() -> None:
    sg = ScalingGroup(
        image="ubuntu:latest",
        name="sg-readiness",
        port=8080,
    )

    assert "readinessProbe" not in _deployed_container_spec(sg)


def test_scaling_group_deploy_uses_default_port() -> None:
    sg = ScalingGroup(
        image="ubuntu:latest",
        name="sg-readiness",
    )

    assert _deployed_container_spec(sg)["port"] == 8080


def test_scaling_group_deploy_omits_port_when_none() -> None:
    sg = ScalingGroup(
        image="ubuntu:latest",
        name="sg-readiness",
        port=None,
    )

    assert "port" not in _deployed_container_spec(sg)
