from __future__ import annotations

from unittest.mock import patch

import pytest

from chalkcompute import Image, Sandbox
from chalkcompute._gen.chalk.sandbox.v1 import service_pb2 as pb
from chalkcompute._sandbox import SandboxClient, SandboxCreationError


class _RecordingSandboxClient:
    _connect = object()

    def __init__(self) -> None:
        self.created_image: str | None = None
        self.create_kwargs: dict[str, object] | None = None

    def _instantiate(self, sandbox: Sandbox, image: str, **kwargs: object) -> None:
        self.created_image = image
        self.create_kwargs = {
            **kwargs,
            "cpu": sandbox._cpu,
            "memory": sandbox._memory,
        }
        sandbox._id = "sandbox-123"


def test_sandbox_run_builds_image_before_create() -> None:
    client = _RecordingSandboxClient()
    image = Image.debian_slim().run_commands("pip install claude")
    sandbox = Sandbox(
        image=image,
        cpu="2",
        memory="4Gi",
        sandbox_client=client,  # type: ignore[arg-type]
    )

    with patch(
        "chalkcompute._sandbox.build_image", return_value="registry/image:built"
    ) as build:
        sandbox.run(poll_interval=0.25, timeout=30)

    assert client.created_image == "registry/image:built"
    assert client.create_kwargs is not None
    assert client.create_kwargs["cpu"] == "2"
    assert client.create_kwargs["memory"] == "4Gi"
    build.assert_called_once_with(
        client._connect,
        image,
        poll_interval=0.25,
        timeout=30,
    )


class _Stub:
    def __init__(self) -> None:
        self.request: pb.CreateSandboxRequest | None = None

    def CreateSandbox(self, request: pb.CreateSandboxRequest, **_kwargs: object):
        self.request = request
        return pb.CreateSandboxResponse(
            sandbox=pb.SandboxInfo(
                id="sandbox-123",
                state="ready",
                created_at="2026-08-03T00:00:00Z",
            )
        )


class _ErrorAfterBuildStub:
    def CreateSandbox(self, request: pb.CreateSandboxRequest, **_kwargs: object):
        return pb.CreateSandboxResponse(
            sandbox=pb.SandboxInfo(
                id="sandbox-123",
                state="building",
                created_at="2026-08-03T00:00:00Z",
                build_id="build-456",
            )
        )

    def GetSandbox(self, request: pb.GetSandboxRequest, **_kwargs: object):
        assert request.sandbox_id == "sandbox-123"
        return pb.GetSandboxResponse(
            sandbox=pb.SandboxInfo(
                id="sandbox-123",
                state="error",
                created_at="2026-08-03T00:00:00Z",
                build_id="build-456",
                status_message="pod failed readiness probe: connection refused",
            )
        )


def test_sandbox_client_create_sends_image_uri_not_image_spec() -> None:
    client = SandboxClient()
    stub = _Stub()
    client._stub = stub  # type: ignore[assignment]
    try:
        with patch(
            "chalkcompute._sandbox.build_image", return_value="registry/image:built"
        ) as build:
            client.create(
                Image.debian_slim().run_commands("echo hello"),
                wait=False,
            )
    finally:
        client.close()

    assert stub.request is not None
    assert stub.request.image == "registry/image:built"
    assert stub.request.WhichOneof("image_source") == "image"
    build.assert_called_once()


def test_sandbox_client_create_propagates_backend_error_details() -> None:
    client = SandboxClient()
    client._stub = _ErrorAfterBuildStub()  # type: ignore[assignment]
    try:
        with pytest.raises(SandboxCreationError) as exc_info:
            with patch("chalkcompute._sandbox.time.sleep"):
                client.create("python:3.14-slim", poll_interval=0, timeout=30)
    finally:
        client.close()

    error = exc_info.value
    assert str(error) == (
        "Sandbox sandbox-123 entered error state during creation: "
        "pod failed readiness probe: connection refused"
    )
    assert error.sandbox_id == "sandbox-123"
    assert error.status == "error"
    assert error.status_message == "pod failed readiness probe: connection refused"
    assert error.build_id == "build-456"
