from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest

import chalkcompute._scaling_group as scaling_group_mod
from chalkcompute import ExecutionBackend, NetworkPolicy, ScalingGroup
from chalkcompute._gen.chalk.container.v1 import service_pb as container_pb
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from tests._fakes import ScalingGroupClient


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (None, None),
        (NetworkPolicy(), container_pb.NetworkPolicy()),
        (
            NetworkPolicy(
                allowed_routes=[NetworkPolicy.Route("10.0.0.0/8", ports=[443])],
                denied_routes=["10.1.0.0/16"],
                allowed_hosts={"example.com": []},
            ),
            container_pb.NetworkPolicy(
                allowed_routes=[
                    container_pb.AllowedRoute(
                        route="10.0.0.0/8",
                        port_ranges=[
                            container_pb.PortRange(start_port=443, end_port=443)
                        ],
                    )
                ],
                denied_routes=["10.1.0.0/16"],
                allowed_hosts={"example.com": container_pb.NetworkPolicyRuleList()},
            ),
        ),
    ],
)
def test_scaling_group_deploy_forwards_spec_fields(
    policy: NetworkPolicy | None,
    expected: container_pb.NetworkPolicy | None,
) -> None:
    client = ScalingGroupClient()
    sg = ScalingGroup(
        image="nginx:alpine",
        name="tagged-group",
        tags={"team": "compute"},
        chalk_identity=True,
        network_policy=policy,
    )
    sg._client = cast(Any, client)

    sg.deploy()

    request = client.requests[0]
    assert isinstance(request, sg_pb.CreateScalingGroupRequest)
    assert request.spec is not None and request.spec.container_spec is not None
    assert dict(request.spec.container_spec.tags) == {"team": "compute"}
    assert request.spec.container_spec.has_field("chalk_workload_identity")
    assert request.spec.container_spec.has_field("network_policy") == (
        expected is not None
    )
    assert request.spec.container_spec.network_policy == expected


@pytest.mark.parametrize(
    ("json", "data", "expected"),
    [
        ({"hello": "world"}, None, {"hello": "world"}),
        ({"ignored": True}, b"raw", b"raw"),
    ],
)
def test_scaling_group_call_sends_pyqwest_content(
    monkeypatch: pytest.MonkeyPatch,
    json: dict[str, object],
    data: bytes | None,
    expected: object,
) -> None:
    client = ScalingGroupClient()
    sg = ScalingGroup(image="nginx:alpine", name="content-test")
    sg._client = cast(Any, client)
    sg.deploy()
    http_client = MagicMock()
    monkeypatch.setattr(scaling_group_mod, "SYNC_HTTP_CLIENT", http_client)

    sg.call("/echo", json=json, data=data)

    assert http_client.execute.call_args.kwargs["content"] == expected


def test_scaling_group_tracing_sets_otel_sampler_env() -> None:
    sg = ScalingGroup(
        image="nginx:alpine", name="traced", env={"APP": "1"}, tracing=True
    )
    assert sg._env == {
        "APP": "1",
        "OTEL_TRACES_SAMPLER": "parentbased_traceidratio",
        "OTEL_TRACES_SAMPLER_ARG": "1.0",
    }


def test_scaling_group_tracing_off_and_default() -> None:
    assert ScalingGroup(image="nginx:alpine", name="off", tracing=False)._env == {
        "OTEL_TRACES_SAMPLER": "always_off"
    }
    assert ScalingGroup(image="nginx:alpine", name="default")._env == {}


@pytest.mark.parametrize(
    "target", [None, ExecutionBackend.ENVIRONMENT, ExecutionBackend.SERVERLESS]
)
def test_scaling_group_deploy_forwards_execution_target(
    target: ExecutionBackend | None,
) -> None:
    client = ScalingGroupClient()
    group = ScalingGroup(image="nginx:alpine", execution_target=target)
    group._client = cast(Any, client)
    group.deploy()

    request = client.requests[0]
    assert isinstance(request, sg_pb.CreateScalingGroupRequest)
    assert request.spec is not None and request.spec.container_spec is not None
    spec = request.spec.container_spec
    assert spec.has_field("execution_target") == (target is not None)
    if target is not None:
        assert (
            spec.execution_target.backend == container_pb.ExecutionBackend[target.name]
        )
