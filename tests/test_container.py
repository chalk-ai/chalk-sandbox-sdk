from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from chalkcompute import AllowedRoute, Container, NetworkPolicy, PortRange
from chalkcompute._gen.chalk.container.v1 import service_connect as container_connect
from chalkcompute._gen.chalk.container.v1 import service_pb2 as container_pb
from chalkcompute._container import ContainerSecurityPolicy, KernelPolicy


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[type[Any], Callable[..., Any], Any, float | None]] = []

    def unary(
        self,
        client_cls: type[Any],
        method: Callable[..., Any],
        request: Any,
        timeout: float | None = 30,
    ) -> container_pb.RunContainerResponse:
        self.calls.append((client_cls, method, request, timeout))
        return container_pb.RunContainerResponse(
            container=container_pb.ContainerResponse(
                id="container-123",
                name="network-test",
                status="Running",
                pod_name="pod-network-test",
            )
        )


def test_network_policy_serializes_allowed_routes() -> None:
    assert NetworkPolicy().to_dict() == {"allowedRoutes": []}

    policy = NetworkPolicy(
        allowed_routes=[
            AllowedRoute(
                cidr="104.20.23.154/32",
                ports=[443, (8000, 9000), PortRange(10000, 10001)],
            ),
            AllowedRoute(route="10.0.0.0/8"),
        ]
    )

    assert policy.to_dict() == {
        "allowedRoutes": [
            {
                "route": "104.20.23.154/32",
                "portRanges": [
                    {"startPort": 443, "endPort": 443},
                    {"startPort": 8000, "endPort": 9000},
                    {"startPort": 10000, "endPort": 10001},
                ],
            },
            {"route": "10.0.0.0/8"},
        ]
    }


def test_network_policy_validates_cidr_and_ports() -> None:
    with pytest.raises(ValueError):
        AllowedRoute(cidr="not-a-cidr")

    with pytest.raises(ValueError):
        PortRange(0)

    with pytest.raises(ValueError):
        PortRange(2000, 1000)


def test_container_run_sends_network_policy() -> None:
    client = _Client()
    container = Container(
        image="ubuntu:latest",
        name="network-test",
        security_policy=ContainerSecurityPolicy(kernel_policy=KernelPolicy.RESTRICTED),
        network_policy=NetworkPolicy(
            allowed_routes=[AllowedRoute(cidr="104.20.23.154/32", ports=[(443, 443)])]
        ),
    )
    container._client = client

    container.run()

    assert len(client.calls) == 1
    client_cls, method, request, timeout = client.calls[0]
    assert client_cls is container_connect.ContainerServiceClientSync
    assert method is container_connect.ContainerServiceClientSync.run_container
    assert timeout == 60
    spec = request.container.spec
    assert spec.network_policy.allowed_routes[0].route == "104.20.23.154/32"
    assert spec.network_policy.allowed_routes[0].port_ranges[0].start_port == 443
    assert spec.network_policy.allowed_routes[0].port_ranges[0].end_port == 443
    assert spec.security_policy.kernel_policy == container_pb.KERNEL_POLICY_RESTRICTED


def test_container_run_sends_tags() -> None:
    client = _Client()
    container = Container(
        image="ubuntu:latest",
        name="tagged-container",
        tags={"team": "accounting", "env": "staging"},
    )
    container._client = client

    container.run()

    assert len(client.calls) == 1
    _, _, request, _ = client.calls[0]
    assert dict(request.container.spec.tags) == {
        "team": "accounting",
        "env": "staging",
    }


def test_container_run_omits_tags_when_unset() -> None:
    client = _Client()
    container = Container(image="ubuntu:latest", name="untagged-container")
    container._client = client

    container.run()

    _, _, request, _ = client.calls[0]
    assert len(request.container.spec.tags) == 0
