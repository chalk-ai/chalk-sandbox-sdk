from __future__ import annotations

from typing import Any

import pytest

from chalkcompute import AllowedRoute, Container, NetworkPolicy, PortRange
from chalkcompute._container import ContainerSecurityPolicy, KernelPolicy


class _Response:
    status_code = 200
    text = ""

    def json(self) -> dict[str, Any]:
        return {
            "container": {
                "id": "container-123",
                "name": "network-test",
                "status": "Running",
                "podName": "pod-network-test",
            }
        }


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any], float]] = []

    def rpc(
        self,
        service: str,
        method: str,
        body: dict[str, Any],
        timeout: float = 30,
    ) -> _Response:
        self.calls.append((service, method, body, timeout))
        return _Response()


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
        security_policy=ContainerSecurityPolicy(
            kernel_policy=KernelPolicy.RESTRICTED
        ),
        network_policy=NetworkPolicy(
            allowed_routes=[
                AllowedRoute(cidr="104.20.23.154/32", ports=[(443, 443)])
            ]
        ),
    )
    container._client = client

    container.run()

    assert len(client.calls) == 1
    service, method, body, timeout = client.calls[0]
    assert service == "chalk.container.v1.ContainerService"
    assert method == "RunContainer"
    assert timeout == 60
    assert body["container"]["spec"]["networkPolicy"] == {
        "allowedRoutes": [
            {
                "route": "104.20.23.154/32",
                "portRanges": [{"startPort": 443, "endPort": 443}],
            }
        ]
    }
    assert body["container"]["spec"]["securityPolicy"] == {
        "kernelPolicy": "RESTRICTED"
    }


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
    _, _, body, _ = client.calls[0]
    assert body["container"]["spec"]["tags"] == {
        "team": "accounting",
        "env": "staging",
    }


def test_exec_binary_not_found_returns_exit_code_127() -> None:
    class _NotFoundResponse:
        status_code = 400
        text = (
            '{"code":"unknown","message":"failed to execute command: OCI runtime exec failed: '
            'exec failed: unable to start container process: exec: \\"nonexistentbinary\\": '
            'executable file not found in $PATH"}'
        )

        def json(self) -> dict[str, Any]:
            return {}

    class _ExecClient(_Client):
        def rpc(self, service: str, method: str, body: dict[str, Any], timeout: float = 30) -> Any:
            if method == "ExecCommand":
                return _NotFoundResponse()
            return super().rpc(service, method, body, timeout)

    from chalkcompute._container import ExecResult
    client = _ExecClient()
    container = Container(image="ubuntu:latest", name="exec-test")
    container._client = client
    container.run()
    container._container_id = "fake-id"

    result = container.exec("nonexistentbinary")
    assert isinstance(result, ExecResult)
    assert result.exit_code == 127
    assert b"executable file not found" in result.stderr


def test_container_run_omits_tags_when_unset() -> None:
    client = _Client()
    container = Container(image="ubuntu:latest", name="untagged-container")
    container._client = client

    container.run()

    _, _, body, _ = client.calls[0]
    assert "tags" not in body["container"]["spec"]
