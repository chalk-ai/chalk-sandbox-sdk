from __future__ import annotations

from typing import Any

import pytest

from chalkcompute import Container, ComputeClass, NetworkPolicy
from chalkcompute._container import ContainerSecurityPolicy, KernelPolicy
from chalkcompute._gen.chalk.container.v1 import service_pb as pb
from chalkcompute._volume import VolumeMount

from ._fakes import FakeConnectClient


def _run_container_calls(client: FakeConnectClient) -> list[Any]:
    return [c for c in client.calls if c.method == "run_container"]


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
    assert call.method == "run_container"
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
    assert rule.match.path.matcher.value == "/v1/"
    assert list(rule.match.method) == ["GET"]
    assert rule.match.headers[0].key.matcher.value == "x-api-key"
    (transform,) = rule.transform
    assert dict(transform.headers) == {"x-added": "yes"}
    assert dict(transform.headers_secrets) == {"authorization": "API_TOKEN"}

    assert spec.security_policy.kernel_policy == pb.KernelPolicy.RESTRICTED
    assert spec.compute_class == pb.ComputeClass.HOST


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
    assert spec.compute_class == pb.ComputeClass.HOST


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
    assert [c.method for c in client.calls if c.method == "stop_container"] == [
        "stop_container"
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
    # Referenced volumes are typed by a backend lookup in run().
    assert c._referenced_volumes == [
        VolumeMount(name="vol-a", mount_path="/data"),
        VolumeMount(name="vol-b", mount_path="/scratch"),
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

    resolve.assert_called_once_with(
        client, VolumeMount(name="my-volume", mount_path="/data")
    )
    spec = client.calls[0].request.container.spec
    assert len(spec.volumes) == 1
    volume = spec.volumes[0]
    assert (volume.name, volume.mount_path, volume.type) == (
        "my-volume",
        "/data",
        "versioned_chalkfs",
    )


@pytest.mark.parametrize("initial_status", ["Running", "Pending"])
def test_container_uploads_lazy_local_files_without_exec_probe(
    tmp_path, initial_status
) -> None:
    from unittest.mock import MagicMock, patch

    from chalkcompute import Image
    from chalkcompute._volume import VolumeClient

    from ._fakes import default_container_handler

    source = tmp_path / "data.txt"
    source.write_text("uploaded content")
    image = Image.debian_slim().add_local_file(str(source), "/assets/data.txt")

    def handle(method, request):
        response = default_container_handler(method, request)
        if method == "run_container":
            response.container.status = initial_status
        return response

    client = FakeConnectClient(handler=handle)
    container = Container(image=image)
    container._client = client
    volume_client = MagicMock(spec=VolumeClient)
    volume_client.upload_lazy_files.return_value = [
        VolumeMount(name="container-files-abc", mount_path="/assets")
    ]

    with (
        patch("chalkcompute._connect.build_image", return_value="img:built"),
        patch.object(VolumeClient, "from_connect", return_value=volume_client),
        patch.object(
            Container,
            "exec",
            side_effect=AssertionError("Startup must not execute commands"),
        ) as execute,
    ):
        assert container.run() is container

    execute.assert_not_called()
    volume_client.upload_lazy_files.assert_called_once_with(
        image, volume_prefix="container-files"
    )
    expected_calls = ["run_container"]
    if initial_status == "Pending":
        expected_calls.append("get_container")
    assert [call.method for call in client.calls] == expected_calls
    spec = client.calls[0].request.container.spec
    assert spec.image == "img:built"
    assert [(mount.name, mount.mount_path) for mount in spec.volumes] == [
        ("container-files-abc", "/assets")
    ]


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
    assert spec.compute_class == pb.ComputeClass.HOST


def test_container_run_omits_compute_class_when_unset() -> None:
    client = FakeConnectClient()
    container = Container(image="ubuntu:latest", name="default-container")
    container._client = client

    container.run()

    spec = client.calls[0].request.container.spec
    assert spec.compute_class == pb.ComputeClass.UNSPECIFIED
