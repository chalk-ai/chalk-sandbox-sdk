from functools import partial

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
    assert (
        factory(execution_target=ExecutionBackend.SERVERLESS)._execution_target
        is ExecutionBackend.SERVERLESS
    )
