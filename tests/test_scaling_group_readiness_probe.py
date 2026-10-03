from __future__ import annotations

import json
from typing import Any, cast

import pytest

from chalkcompute import ReadinessProbe, ScalingGroup, StartupProbe
from chalkcompute._gen.chalk.container.v1 import service_pb as container_pb
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from tests._fakes import FakeMethod, StubbedConnectClient


class _CapturingClient(StubbedConnectClient):
    def __init__(self) -> None:
        self.requests: list[sg_pb.CreateScalingGroupRequest] = []

    def call(
        self,
        method: FakeMethod,
        request: sg_pb.CreateScalingGroupRequest,
        **_: Any,
    ) -> sg_pb.CreateScalingGroupResponse:
        assert method.name == "create_scaling_group"
        self.requests.append(request)
        return sg_pb.CreateScalingGroupResponse(
            scaling_group=sg_pb.ScalingGroupResponse(
                id="sg-1",
                name="sg-readiness",
                status="Running",
                ready_replicas=1,
                available_replicas=1,
            )
        )


def _deployed_container_spec(sg: ScalingGroup) -> container_pb.ChalkContainerSpec:
    client = _CapturingClient()
    sg._client = cast(Any, client)
    sg.deploy()
    spec = client.requests[0].spec
    assert spec is not None and spec.container_spec is not None
    return spec.container_spec


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

    probe = container_spec.readiness_probe
    assert probe is not None and probe.check is not None
    assert probe.check.value.path == "/readyz"
    assert container_spec.port == 8080


def test_scaling_group_deploy_omits_readiness_probe_by_default() -> None:
    sg = ScalingGroup(
        image="ubuntu:latest",
        name="sg-readiness",
        port=8080,
    )

    assert _deployed_container_spec(sg).readiness_probe is None


def test_scaling_group_deploy_uses_default_port() -> None:
    sg = ScalingGroup(
        image="ubuntu:latest",
        name="sg-readiness",
    )

    assert _deployed_container_spec(sg).port == 8080


def test_scaling_group_deploy_omits_port_when_none() -> None:
    sg = ScalingGroup(
        image="ubuntu:latest",
        name="sg-readiness",
        port=None,
    )

    assert not _deployed_container_spec(sg).has_field("port")


@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        (StartupProbe.http("/health"), {"http": {"path": "/health"}}),
        (StartupProbe.http(), {"http": {}}),
        (
            StartupProbe.grpc("/grpc.health.v1.Health/Check"),
            {"grpc": {"method": "/grpc.health.v1.Health/Check"}},
        ),
        (StartupProbe.grpc(), {"grpc": {}}),
    ],
)
def test_scaling_group_deploy_includes_startup_probe(
    probe: StartupProbe, expected: dict[str, Any]
) -> None:
    protocol = "grpc" if probe._kind == "grpc" else "http"
    sg = ScalingGroup(image="ubuntu:latest", protocol=protocol, startup_probe=probe)
    spec = _deployed_container_spec(sg)
    assert spec.startup_probe == container_pb.StartupProbe.from_json(
        json.dumps(expected)
    )
    assert spec.protocol == protocol
    assert spec.readiness_probe is None


def test_grpc_startup_probe_requires_grpc_protocol() -> None:
    with pytest.raises(ValueError, match="requires protocol='grpc'"):
        ScalingGroup(image="ubuntu:latest", startup_probe=StartupProbe.grpc())


def test_http_startup_probe_rejects_grpc_protocol() -> None:
    with pytest.raises(ValueError, match="requires an HTTP protocol"):
        ScalingGroup(
            image="ubuntu:latest", protocol="grpc", startup_probe=StartupProbe.http()
        )


def test_startup_probe_requires_port() -> None:
    with pytest.raises(ValueError, match="requires port"):
        ScalingGroup(
            image="ubuntu:latest", port=None, startup_probe=StartupProbe.http()
        )


def test_readiness_probe_does_not_enable_startup_probe() -> None:
    sg = ScalingGroup(
        image="ubuntu:latest", readiness_probe=ReadinessProbe.http("/ready")
    )
    assert _deployed_container_spec(sg).startup_probe is None


def test_scaling_group_empty_initializes_startup_probe() -> None:
    sg = ScalingGroup._make_empty()
    assert sg._startup_probe is None
