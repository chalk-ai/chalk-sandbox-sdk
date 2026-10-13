from functools import partial
from typing import Any, cast

import pytest

from chalkcompute import (
    Container,
    ExecutionBackend,
    RemoteClass,
    RemoteFunction,
    Sandbox,
    ScalingGroup,
    method,
)
from chalkcompute._gen.chalk.container.v1 import service_pb as container_pb
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from tests._fakes import FakeConnectClient


def identity(value: int) -> int:
    return value


class Worker:
    @method
    def identity(self, value: int) -> int:
        return value


@pytest.mark.parametrize(
    "factory",
    [
        partial(Container, image="ubuntu:latest"),
        partial(Sandbox, image="ubuntu:latest"),
        partial(ScalingGroup, image="ubuntu:latest"),
        partial(RemoteFunction, identity),
        partial(RemoteClass, Worker),
    ],
    ids=["container", "sandbox", "scaling_group", "function", "class"],
)
def test_execution_target_uses_shared_validation(factory) -> None:
    with pytest.raises(TypeError, match="execution_target must be an ExecutionBackend"):
        factory(execution_target="serverless")


@pytest.mark.parametrize(
    "target", [None, ExecutionBackend.ENVIRONMENT, ExecutionBackend.SERVERLESS]
)
def test_scaling_group_execution_target(target: ExecutionBackend | None) -> None:
    client = FakeConnectClient(
        handler=lambda _method, _request: sg_pb.CreateScalingGroupResponse(
            scaling_group=sg_pb.ScalingGroupResponse(
                id="sg-1",
                name="sg-placement",
                status="Running",
                ready_replicas=1,
                available_replicas=1,
            )
        )
    )
    group = ScalingGroup(image="ubuntu:latest", execution_target=target)
    group._client = cast(Any, client)
    group.deploy()

    spec = client.calls[0].request.spec.container_spec
    assert spec.has_field("execution_target") == (target is not None)
    if target is not None:
        assert (
            spec.execution_target.backend == container_pb.ExecutionBackend[target.name]
        )
