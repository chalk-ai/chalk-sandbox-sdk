import pytest
import chalkcompute as cc
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as pb


@pytest.mark.parametrize("value", [0, -1, 1_000_001, True, 1.5])
def test_invalid_allowance(value):
    with pytest.raises((ValueError, TypeError)):
        cc.GatewayAdmissionPolicy(value)


@pytest.mark.parametrize("lifetime", ["network", "execution"])
def test_wire_policy(lifetime):
    policy = cc.GatewayAdmissionPolicy(
        7, lifetime=lifetime, max_scale_from_zero_requests=3
    )
    wire = policy.to_proto()
    assert wire.max_inflight_requests_per_replica == 7
    assert wire.lifetime == (
        pb.GatewayAdmissionLifetime.NETWORK
        if lifetime == "network"
        else pb.GatewayAdmissionLifetime.EXECUTION
    )
    assert wire.max_scale_from_zero_requests == 3


@pytest.mark.parametrize("use_class", [False, True])
def test_function_and_class_deployment(monkeypatch, use_class):
    from tests.test_policies import _CapturingClient

    policy = cc.GatewayAdmissionPolicy(7)
    if use_class:

        class Worker:
            @cc.method
            def run(self) -> None:
                pass

        deployed = cc.RemoteClass(Worker, gateway_admission=policy)
        monkeypatch.setattr(
            cc.RemoteClass,
            "_build_deploy_image",
            lambda _: (cc.Image("image:test"), []),
        )
    else:

        def run(x: int) -> int:
            return x

        deployed = cc.RemoteFunction(run, gateway_admission=policy)
        monkeypatch.setattr(
            cc.RemoteFunction, "_build_deploy_image", lambda _: cc.Image("image:test")
        )
    client = _CapturingClient()
    deployed._client = client
    deployed.deploy()
    assert client.requests
    for request in client.requests:
        assert request.spec.gateway_admission.max_inflight_requests_per_replica == 7
        assert (
            request.spec.gateway_admission.lifetime
            == pb.GatewayAdmissionLifetime.EXECUTION
        )


def test_execution_requires_native_grpc():
    with pytest.raises(ValueError, match="protocol='grpc'"):
        cc.ScalingGroup(
            image=cc.Image("image:test"), gateway_admission=cc.GatewayAdmissionPolicy(2)
        )
