from __future__ import annotations

from typing import Any

import pytest

from chalkcompute import Container, ComputeClass, NetworkPolicy
from chalkcompute._container import ContainerSecurityPolicy, KernelPolicy
from chalkcompute._gen.chalk.container.v1 import service_pb2 as pb

from ._fakes import FakeConnectClient


def _run_container_calls(client: FakeConnectClient) -> list[Any]:
    return [c for c in client.calls if c.method == "RunContainer"]


def test_network_policy_serializes_allowed_routes() -> None:
    assert NetworkPolicy().to_dict() == {"allowedRoutes": []}

    policy = NetworkPolicy(
        allowed_routes=[
            NetworkPolicy.Route(
                "104.20.23.154/32",
                ports=[443, (8000, 9000), (10000, 10001)],
            ),
            NetworkPolicy.Route("10.0.0.0/8"),
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


def test_network_policy_validates_ports_and_matchers() -> None:
    with pytest.raises(ValueError):
        NetworkPolicy.Route("")

    with pytest.raises(ValueError):
        NetworkPolicy.Route("10.0.0.0/8", ports=[0])

    with pytest.raises(ValueError):
        NetworkPolicy.Route("10.0.0.0/8", ports=[(2000, 1000)])

    with pytest.raises(ValueError):
        NetworkPolicy.Matcher()

    with pytest.raises(ValueError):
        NetworkPolicy.Matcher(exact="x", regex="x")


def test_container_is_deprecated() -> None:
    with pytest.warns(DeprecationWarning, match="Use `Sandbox` instead"):
        Container(image="ubuntu:latest")


def test_host_container_run_sends_network_policy() -> None:
    client = FakeConnectClient()
    container = Container(
        image="ubuntu:latest",
        name="network-test",
        compute_class=ComputeClass.HOST,
        security_policy=ContainerSecurityPolicy(kernel_policy=KernelPolicy.RESTRICTED),
        network_policy=NetworkPolicy(
            allowed_routes=[NetworkPolicy.Route("104.20.23.154/32", ports=[443])],
            denied_routes=["169.254.0.0/16"],
            allowed_hosts={
                "api.example.com": [
                    NetworkPolicy.Rule(
                        match=NetworkPolicy.Match(
                            path=NetworkPolicy.Matcher(starts_with="/v1/"),
                            methods=["GET"],
                            headers=[
                                NetworkPolicy.KeyValueMatcher(
                                    key=NetworkPolicy.Matcher(exact="x-api-key")
                                )
                            ],
                        ),
                        transforms=[
                            NetworkPolicy.Transformer(
                                headers={"x-added": "yes"},
                                headers_secrets={"authorization": "API_TOKEN"},
                            )
                        ],
                        forward_url="https://proxy.example.com/api",
                    )
                ]
            },
        ),
    )
    container._client = client

    container.run()

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call.method == "RunContainer"
    assert call.timeout == 60
    assert isinstance(call.request, pb.RunContainerRequest)
    spec = call.request.container.spec

    policy = spec.network_policy
    assert len(policy.allowed_routes) == 1
    assert policy.allowed_routes[0].route == "104.20.23.154/32"
    assert [
        (r.start_port, r.end_port) for r in policy.allowed_routes[0].port_ranges
    ] == [(443, 443)]
    assert list(policy.denied_routes) == ["169.254.0.0/16"]

    assert set(policy.allowed_hosts) == {"api.example.com"}
    (rule,) = policy.allowed_hosts["api.example.com"].rules
    assert rule.forward_url == "https://proxy.example.com/api"
    assert rule.match.path.starts_with == "/v1/"
    assert list(rule.match.method) == ["GET"]
    assert rule.match.headers[0].key.exact == "x-api-key"
    (transform,) = rule.transform
    assert dict(transform.headers) == {"x-added": "yes"}
    assert dict(transform.headers_secrets) == {"authorization": "API_TOKEN"}

    assert spec.security_policy.kernel_policy == pb.KERNEL_POLICY_RESTRICTED
    assert spec.compute_class == pb.COMPUTE_CLASS_HOST


def test_host_container_network_policy_accepts_allowed_hosts_list() -> None:
    client = FakeConnectClient()
    container = Container(
        image="ubuntu:latest",
        name="host-list-network-test",
        compute_class=ComputeClass.HOST,
        network_policy=NetworkPolicy(
            allowed_hosts=[
                "api.openai.com",
                "*.anthropic.com",
            ],
        ),
    )
    container._client = client

    container.run()

    spec = client.calls[0].request.container.spec
    policy = spec.network_policy
    assert len(policy.allowed_routes) == 0
    assert set(policy.allowed_hosts) == {"api.openai.com", "*.anthropic.com"}
    assert len(policy.allowed_hosts["api.openai.com"].rules) == 0
    assert len(policy.allowed_hosts["*.anthropic.com"].rules) == 0
    assert spec.compute_class == pb.COMPUTE_CLASS_HOST


def test_container_run_sends_tags() -> None:
    client = FakeConnectClient()
    container = Container(
        image="ubuntu:latest",
        name="tagged-container",
        tags={"team": "accounting", "env": "staging"},
    )
    container._client = client

    container.run()

    assert len(client.calls) == 1
    spec = client.calls[0].request.container.spec
    assert dict(spec.tags) == {"team": "accounting", "env": "staging"}


def test_lifetime_valid_formats_accepted() -> None:
    for lifetime in ["30s", "1.5s", "300s", "0.5s", "3600s"]:
        Container(image="ubuntu:latest", lifetime=lifetime)  # must not raise


def test_lifetime_invalid_formats_rejected() -> None:
    for lifetime in ["not-a-duration", "5m", "2h", "30", "abc", ""]:
        with pytest.raises(ValueError, match="lifetime"):
            Container(image="ubuntu:latest", lifetime=lifetime)


def test_run_twice_raises_container_error() -> None:
    client = FakeConnectClient()
    container = Container(image="ubuntu:latest", name="double-run-test")
    container._client = client

    container.run()

    from chalkcompute._container import ContainerError

    with pytest.raises(ContainerError, match="already running"):
        container.run()

    assert len(_run_container_calls(client)) == 1  # second run() never reached the RPC


def test_stop_then_run_again_is_allowed() -> None:
    """stop() clears _container_id so a subsequent run() must succeed."""
    client = FakeConnectClient()
    container = Container(image="ubuntu:latest", name="restart-test")
    container._client = client

    container.run()
    container.stop()
    container.run()  # must not raise

    assert len(_run_container_calls(client)) == 2
    assert [c.method for c in client.calls if c.method == "StopContainer"] == [
        "StopContainer"
    ]


def test_container_run_omits_tags_when_unset() -> None:
    client = FakeConnectClient()
    container = Container(image="ubuntu:latest", name="untagged-container")
    container._client = client

    container.run()

    spec = client.calls[0].request.container.spec
    assert not spec.tags


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

    client = FakeConnectClient()
    container = Container(image="ubuntu:latest", name="vol-test").mount_volume(
        "my-volume", "/data"
    )
    container._client = client

    # A referenced volume must exist at run() time.
    from chalkcompute._volume import VolumeMount

    with patch(
        "chalkcompute._container.referenced_volume_mount",
        return_value=VolumeMount(name="my-volume", mount_path="/data"),
    ) as resolve:
        container.run()

    resolve.assert_called_once_with(client, "my-volume", "/data")
    spec = client.calls[0].request.container.spec
    assert len(spec.volumes) == 1
    volume = spec.volumes[0]
    assert (volume.name, volume.mount_path, volume.type) == (
        "my-volume",
        "/data",
        "versioned_chalkfs",
    )


def test_container_run_sends_compute_class() -> None:
    client = FakeConnectClient()
    container = Container(
        image="ubuntu:latest",
        name="host-container",
        compute_class=ComputeClass.HOST,
    )
    container._client = client

    container.run()

    spec = client.calls[0].request.container.spec
    assert spec.compute_class == pb.COMPUTE_CLASS_HOST


def test_container_run_omits_compute_class_when_unset() -> None:
    client = FakeConnectClient()
    container = Container(image="ubuntu:latest", name="default-container")
    container._client = client

    container.run()

    spec = client.calls[0].request.container.spec
    assert spec.compute_class == pb.COMPUTE_CLASS_UNSPECIFIED
