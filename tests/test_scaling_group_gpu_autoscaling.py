from __future__ import annotations

from typing import Any, cast

import pytest

from chalkcompute import ScalingGroup


class _Response:
    status_code = 200
    text = ""

    def json(self) -> dict[str, Any]:
        return {
            "scalingGroup": {
                "id": "sg-1",
                "name": "sg-gpu",
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


def _deployed_scaling_spec(sg: ScalingGroup) -> dict[str, Any]:
    client = _CapturingClient()
    sg._client = cast(Any, client)
    sg.deploy()
    return client.requests[0]["spec"]["scalingSpec"]


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

    assert _deployed_scaling_spec(sg)["gpuUtilizationTrigger"] == {
        "targetUtilizationPercentage": 70
    }


def test_deploy_omits_gpu_utilization_trigger_by_default() -> None:
    sg = ScalingGroup(
        image="ubuntu:latest",
        name="sg-gpu",
        port=8080,
        gpu="nvidia-l4",
    )

    assert "gpuUtilizationTrigger" not in _deployed_scaling_spec(sg)


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
    assert scaling_spec["targetCpuUtilizationPercentage"] == 60
    assert scaling_spec["gpuUtilizationTrigger"] == {"targetUtilizationPercentage": 70}


@pytest.mark.parametrize("percentage", [0, -1, 101])
def test_gpu_target_out_of_range_rejected(percentage: int) -> None:
    with pytest.raises(ValueError, match="must be in range 1-100"):
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
