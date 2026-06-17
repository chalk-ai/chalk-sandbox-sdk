from __future__ import annotations

from typing import Any

import pytest

from chalkcompute import AllowedRoute, Container, ComputeClass, NetworkPolicy, PortRange
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
        security_policy=ContainerSecurityPolicy(kernel_policy=KernelPolicy.RESTRICTED),
        network_policy=NetworkPolicy(
            allowed_routes=[AllowedRoute(cidr="104.20.23.154/32", ports=[(443, 443)])]
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
    assert body["container"]["spec"]["securityPolicy"] == {"kernelPolicy": "RESTRICTED"}


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


def test_lifetime_valid_formats_accepted() -> None:
    for lifetime in ["30s", "1.5s", "300s", "0.5s", "3600s"]:
        Container(image="ubuntu:latest", lifetime=lifetime)  # must not raise


def test_lifetime_invalid_formats_rejected() -> None:
    for lifetime in ["not-a-duration", "5m", "2h", "30", "abc", ""]:
        with pytest.raises(ValueError, match="lifetime"):
            Container(image="ubuntu:latest", lifetime=lifetime)


def test_run_twice_raises_container_error() -> None:
    client = _Client()
    container = Container(image="ubuntu:latest", name="double-run-test")
    container._client = client

    container.run()

    from chalkcompute._container import ContainerError

    with pytest.raises(ContainerError, match="already running"):
        container.run()

    assert len(client.calls) == 1  # second run() never reached the RPC


def test_stop_then_run_again_is_allowed() -> None:
    """stop() clears _container_id so a subsequent run() must succeed."""

    class _StopResponse:
        status_code = 200
        text = ""

        def json(self) -> dict[str, Any]:
            return {}

    call_log: list[tuple[str, str]] = []

    class _TrackingClient(_Client):
        def rpc(
            self, service: str, method: str, body: dict[str, Any], timeout: float = 30
        ) -> Any:
            call_log.append((service, method))
            if method == "StopContainer":
                return _StopResponse()
            return super().rpc(service, method, body, timeout)

    client = _TrackingClient()
    container = Container(image="ubuntu:latest", name="restart-test")
    container._client = client

    container.run()
    container.stop()
    container.run()  # must not raise

    run_calls = [m for _, m in call_log if m == "RunContainer"]
    assert len(run_calls) == 2


def test_container_run_omits_tags_when_unset() -> None:
    client = _Client()
    container = Container(image="ubuntu:latest", name="untagged-container")
    container._client = client

    container.run()

    _, _, body, _ = client.calls[0]
    assert "tags" not in body["container"]["spec"]


def test_mount_volume_appends_and_chains() -> None:
    c = (
        Container(image="ubuntu:latest")
        .mount_volume("vol-a", "/data")
        .mount_volume("vol-b", "/scratch")
    )
    # Referenced volumes carry no type until deploy time; they are stored as
    # raw (name, mount_path) references and typed by a backend lookup in run().
    assert c._referenced_volumes == [
        ("vol-a", "/data"),
        ("vol-b", "/scratch"),
    ]


def test_mount_volume_sent_in_run_container_rpc() -> None:
    from unittest.mock import patch

    client = _Client()
    container = Container(image="ubuntu:latest", name="vol-test").mount_volume(
        "my-volume", "/data"
    )
    container._client = client

    # The referenced volume's type comes from a backend lookup at run() time;
    # here the backend reports a non-versioned (chalkfs) volume.
    with patch(
        "chalkcompute._container.resolve_referenced_volume_type",
        return_value="chalkfs",
    ) as resolve:
        container.run()

    resolve.assert_called_once_with(client, "my-volume")
    _, _, body, _ = client.calls[0]
    assert body["container"]["spec"]["volumes"] == [
        {"name": "my-volume", "mountPath": "/data", "type": "chalkfs"}
    ]


def test_container_run_sends_compute_class() -> None:
    client = _Client()
    container = Container(
        image="ubuntu:latest",
        name="host-container",
        compute_class=ComputeClass.HOST,
    )
    container._client = client

    container.run()

    _, _, body, _ = client.calls[0]
    assert body["container"]["spec"]["computeClass"] == "COMPUTE_CLASS_HOST"


def test_container_run_omits_compute_class_when_unset() -> None:
    client = _Client()
    container = Container(image="ubuntu:latest", name="default-container")
    container._client = client

    container.run()

    _, _, body, _ = client.calls[0]
    assert "computeClass" not in body["container"]["spec"]
