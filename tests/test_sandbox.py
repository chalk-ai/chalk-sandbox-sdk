from __future__ import annotations

from unittest.mock import patch

import pytest
from chalkcompute import Image, Sandbox
from chalkcompute._gen.chalk.sandbox.v2 import process_service_pb2 as process_pb
from chalkcompute._gen.chalk.sandbox.v2 import service_pb2 as pb
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
            "volumes": sandbox._volumes,
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
                status=pb.SANDBOX_STATUS_RUNNING,
                created_at={"seconds": 1},
            )
        )


class _ErrorAfterBuildStub:
    def CreateSandbox(self, request: pb.CreateSandboxRequest, **_kwargs: object):
        return pb.CreateSandboxResponse(
            sandbox=pb.SandboxInfo(
                id="sandbox-123",
                status=pb.SANDBOX_STATUS_PENDING,
                created_at={"seconds": 1},
            )
        )

    def GetSandbox(self, request: pb.GetSandboxRequest, **_kwargs: object):
        assert request.id == "sandbox-123"
        return pb.GetSandboxResponse(
            sandbox=pb.SandboxInfo(
                id="sandbox-123",
                status=pb.SANDBOX_STATUS_FAILED,
                created_at={"seconds": 1},
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
    assert stub.request.spec.image == "registry/image:built"
    build.assert_called_once()


def test_sandbox_client_create_can_request_chalk_identity() -> None:
    client = SandboxClient()
    stub = _Stub()
    client._stub = stub  # type: ignore[assignment]
    try:
        client.create("python:3.14-slim", chalk_identity=True, wait=False)
    finally:
        client.close()

    assert stub.request is not None
    assert stub.request.spec.HasField("chalk_workload_identity")


def test_sandbox_client_create_omits_chalk_identity_by_default() -> None:
    client = SandboxClient()
    stub = _Stub()
    client._stub = stub  # type: ignore[assignment]
    try:
        client.create("python:3.14-slim", wait=False)
    finally:
        client.close()

    assert stub.request is not None
    assert not stub.request.spec.HasField("chalk_workload_identity")


def test_sandbox_rejects_non_bool_chalk_identity() -> None:
    with pytest.raises(TypeError, match="chalk_identity must be a bool"):
        Sandbox(
            image="python:3.14-slim",
            chalk_identity="yes",  # type: ignore[arg-type]
        )


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
        "Sandbox sandbox-123 entered failed state during creation: "
        "pod failed readiness probe: connection refused"
    )
    assert error.sandbox_id == "sandbox-123"
    assert error.status == "failed"
    assert error.status_message == "pod failed readiness probe: connection refused"
    assert error.build_id is None


class _ProcessStub:
    def __init__(self) -> None:
        self.request: process_pb.OpenSessionRequest | None = None

    def OpenSession(self, requests, **_kwargs: object):
        self.request = next(requests)
        return iter(
            [
                process_pb.OpenSessionResponse(
                    session_attached=process_pb.SessionAttached(
                        session_id="session-123"
                    )
                ),
                process_pb.OpenSessionResponse(
                    output_data=process_pb.OutputData(
                        stream=process_pb.OutputData.STREAM_STDOUT,
                        data=b"hello\\n",
                    )
                ),
                process_pb.OpenSessionResponse(
                    process_exited=process_pb.ProcessExited(exit_code=0)
                ),
            ]
        )


def test_sandbox_exec_uses_v2_process_service() -> None:
    client = SandboxClient()
    stub = _ProcessStub()
    client._process_stub = stub  # type: ignore[assignment]
    try:
        result = Sandbox(client=client, sandbox_id="sandbox-123").exec("echo", "hello")
    finally:
        client.close()

    assert result.stdout == b"hello\\n"
    assert result.stderr == b""
    assert result.exit_code == 0
    assert stub.request is not None
    assert stub.request.new_process.sandbox_id == "sandbox-123"
    assert stub.request.new_process.command == "echo"
    assert list(stub.request.new_process.args) == ["hello"]


def test_sandbox_referenced_volume_is_existence_checked() -> None:
    from chalkcompute._volume import VolumeMount

    client = _RecordingSandboxClient()
    sandbox = Sandbox(
        image="ubuntu:latest",
        volumes=[("my-data", "/data")],
        sandbox_client=client,  # type: ignore[arg-type]
    )

    with patch(
        "chalkcompute._sandbox.referenced_volume_mount",
        return_value=VolumeMount(name="my-data", mount_path="/data"),
    ) as resolve:
        sandbox.run()

    resolve.assert_called_once_with(client._connect, "my-data", "/data")
    assert client.create_kwargs is not None
    assert client.create_kwargs["volumes"] == [
        VolumeMount(name="my-data", mount_path="/data")
    ]


def test_sandbox_missing_referenced_volume_fails_run() -> None:
    import pytest

    from chalkcompute._volume import VolumeError

    client = _RecordingSandboxClient()
    sandbox = Sandbox(
        image="ubuntu:latest",
        volumes=[("nope", "/data")],
        sandbox_client=client,  # type: ignore[arg-type]
    )

    with patch(
        "chalkcompute._sandbox.referenced_volume_mount",
        side_effect=VolumeError("Referenced volume 'nope' was not found."),
    ):
        with pytest.raises(VolumeError, match="nope"):
            sandbox.run()


def test_sandbox_uploads_lazy_local_files_to_a_volume(tmp_path) -> None:
    from unittest.mock import MagicMock

    from chalkcompute._volume import VolumeClient, VolumeMount

    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text("print('hi')\n")

    client = _RecordingSandboxClient()
    image = Image.debian_slim().add_local_dir(str(src), "/app")
    sandbox = Sandbox(
        image=image,
        sandbox_client=client,  # type: ignore[arg-type]
    )

    volume_client = MagicMock(spec=VolumeClient)
    volume_client.upload_lazy_files.return_value = [
        VolumeMount(name="sandbox-files-abc", mount_path="/app")
    ]

    with (
        patch("chalkcompute._sandbox.build_image", return_value="img:built"),
        patch.object(VolumeClient, "from_connect", return_value=volume_client),
    ):
        sandbox.run()

    volume_client.upload_lazy_files.assert_called_once_with(
        image, volume_prefix="sandbox-files"
    )
    assert client.create_kwargs is not None
    assert client.create_kwargs["volumes"] == [
        VolumeMount(name="sandbox-files-abc", mount_path="/app")
    ]
