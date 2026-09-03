from __future__ import annotations

from typing import Any, cast

import pytest

from chalkcompute import ScalingGroup
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
                name="sg-gpu",
                status="Running",
                ready_replicas=1,
                available_replicas=1,
            )
        )


def _deployed_scaling_spec(sg: ScalingGroup) -> sg_pb.ScalingSpec:
    client = _CapturingClient()
    sg._client = cast(Any, client)
    sg.deploy()
    spec = client.requests[0].spec
    assert spec is not None and spec.scaling_spec is not None
    return spec.scaling_spec


def test_deploy_includes_gpu_utilization_trigger() -> None:
    sg = ScalingGroup(
        image="ubuntu:latest",
        name="sg-gpu",
        port=8080,
        gpu="nvidia-l4",
        min_replicas=1,
        max_replicas=8,
        target_gpu_utilization_percentage=70,
    )

    trigger = _deployed_scaling_spec(sg).gpu_utilization_trigger
    assert trigger is not None
    assert trigger.target_utilization_percentage == 70


def test_deploy_omits_gpu_utilization_trigger_by_default() -> None:
    sg = ScalingGroup(
        image="ubuntu:latest",
        name="sg-gpu",
        port=8080,
        gpu="nvidia-l4",
    )

    assert _deployed_scaling_spec(sg).gpu_utilization_trigger is None


def test_gpu_and_cpu_targets_are_both_sent() -> None:
    sg = ScalingGroup(
        image="ubuntu:latest",
        name="sg-gpu",
        port=8080,
        gpu="nvidia-l4",
        min_replicas=1,
        max_replicas=8,
        target_cpu_utilization_percentage=60,
        target_gpu_utilization_percentage=70,
    )

    scaling_spec = _deployed_scaling_spec(sg)
    assert scaling_spec.target_cpu_utilization_percentage == 60
    assert scaling_spec.gpu_utilization_trigger is not None
    assert scaling_spec.gpu_utilization_trigger.target_utilization_percentage == 70


@pytest.mark.parametrize("percentage", [0, -1, 101])
def test_gpu_target_out_of_range_rejected(percentage: int) -> None:
    with pytest.raises(ValueError, match="must be between 1 and 100"):
        ScalingGroup(
            image="ubuntu:latest",
            gpu="nvidia-l4",
            target_gpu_utilization_percentage=percentage,
        )


def test_gpu_target_without_gpu_rejected() -> None:
    with pytest.raises(ValueError, match="requires gpu to be set"):
        ScalingGroup(image="ubuntu:latest", target_gpu_utilization_percentage=70)


def test_gpu_target_with_scale_to_zero_rejected() -> None:
    """A group at zero replicas emits no GPU series, so the trigger could never
    observe the nonzero sample it would need to scale back up."""
    with pytest.raises(ValueError, match="requires min_replicas >= 1"):
        ScalingGroup(
            image="ubuntu:latest",
            gpu="nvidia-l4",
            min_replicas=0,
            target_gpu_utilization_percentage=70,
        )


def test_gpu_target_allowed_while_group_is_disabled() -> None:
    sg = ScalingGroup(
        image="ubuntu:latest",
        name="sg-gpu-disabled",
        gpu="nvidia-l4",
        min_replicas=0,
        max_replicas=0,
        target_gpu_utilization_percentage=70,
    )

    scaling_spec = _deployed_scaling_spec(sg)
    assert scaling_spec.min_replicas == 0
    assert scaling_spec.max_replicas == 0
    assert scaling_spec.gpu_utilization_trigger is not None
    assert scaling_spec.gpu_utilization_trigger.target_utilization_percentage == 70
