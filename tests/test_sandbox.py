from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from chalkcompute import Image, Sandbox
from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError
from protobuf import Oneof
from protobuf.wkt import Timestamp
from chalkcompute._containerspec import ContainerSpec
from chalkcompute._gen.chalk.sandbox.v2 import process_service_pb as process_pb
from chalkcompute._gen.chalk.sandbox.v2 import service_pb as pb
from chalkcompute._sandbox import SandboxClient, SandboxCreationError
from chalkcompute._volume import VolumeMount


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

    def create_sandbox(self, request: pb.CreateSandboxRequest, **_kwargs: object):
        self.request = request
        return pb.CreateSandboxResponse(
            sandbox=pb.SandboxInfo(
                id="sandbox-123",
                status=pb.SandboxStatus.RUNNING,
                created_at=Timestamp(seconds=1),
            )
        )


class _ErrorAfterBuildStub:
    def create_sandbox(self, request: pb.CreateSandboxRequest, **_kwargs: object):
        return pb.CreateSandboxResponse(
            sandbox=pb.SandboxInfo(
                id="sandbox-123",
                status=pb.SandboxStatus.PENDING,
                created_at=Timestamp(seconds=1),
            )
        )

    def get_sandbox(self, request: pb.GetSandboxRequest, **_kwargs: object):
        assert request.id == "sandbox-123"
        return pb.GetSandboxResponse(
            sandbox=pb.SandboxInfo(
                id="sandbox-123",
                status=pb.SandboxStatus.FAILED,
                created_at=Timestamp(seconds=1),
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
    assert stub.request.source.value.image == "registry/image:built"
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
    assert stub.request.source.value.has_field("chalk_workload_identity")


def test_sandbox_client_create_omits_chalk_identity_by_default() -> None:
    client = SandboxClient()
    stub = _Stub()
    client._stub = stub  # type: ignore[assignment]
    try:
        client.create("python:3.14-slim", wait=False)
    finally:
        client.close()

    assert stub.request is not None
    assert not stub.request.source.value.has_field("chalk_workload_identity")


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

    def open_session(self, requests, **_kwargs: object):
        self.request = next(requests)
        return iter(
            [
                process_pb.OpenSessionResponse(
                    event=Oneof(
                        "session_attached",
                        process_pb.SessionAttached(session_id="session-123"),
                    )
                ),
                process_pb.OpenSessionResponse(
                    event=Oneof(
                        "output_data",
                        process_pb.OutputData(
                            stream=process_pb.OutputData.Stream.STDOUT,
                            data=b"hello\\n",
                        ),
                    )
                ),
                process_pb.OpenSessionResponse(
                    event=Oneof("process_exited", process_pb.ProcessExited(exit_code=0))
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
    assert stub.request.command.value.sandbox_id == "sandbox-123"
    assert stub.request.command.value.command == "echo"
    assert list(stub.request.command.value.args) == ["hello"]


class _ReattachProcessStub:
    def __init__(self) -> None:
        self.calls = 0

    def open_session(self, requests, **_kwargs: object):
        next(requests)
        self.calls += 1
        if self.calls == 1:
            yield process_pb.OpenSessionResponse(
                seq=1,
                event=Oneof(
                    "session_attached",
                    process_pb.SessionAttached(session_id="session-123"),
                ),
            )
            yield process_pb.OpenSessionResponse(
                seq=2,
                event=Oneof(
                    "output_data",
                    process_pb.OutputData(
                        stream=process_pb.OutputData.Stream.STDOUT,
                        data=b"hello",
                    ),
                ),
            )
            raise ConnectRpcError(Code.UNAVAILABLE, "disconnected")

        yield process_pb.OpenSessionResponse(
            seq=2,
            event=Oneof(
                "output_data",
                process_pb.OutputData(
                    stream=process_pb.OutputData.Stream.STDOUT,
                    data=b"hello",
                ),
            ),
        )
        yield process_pb.OpenSessionResponse(
            seq=3,
            event=Oneof("process_exited", process_pb.ProcessExited(exit_code=0)),
        )


def test_sandbox_reattach() -> None:
    client = SandboxClient()
    client._process_stub = _ReattachProcessStub()  # type: ignore[assignment]
    try:
        with patch("chalkcompute._sandbox.time.sleep"):
            result = Sandbox(client=client, sandbox_id="sandbox-123").exec("echo")
    finally:
        client.close()

    assert result.stdout == b"hello"
    assert result.exit_code == 0


class _TerminatedStub:
    def create_sandbox(self, request: pb.CreateSandboxRequest, **_kwargs: object):
        return pb.CreateSandboxResponse(
            sandbox=pb.SandboxInfo(
                id="sandbox-123",
                status=pb.SandboxStatus.TERMINATED,
                created_at=Timestamp(seconds=1),
                status_message="Exit code 0",
            )
        )


def test_terminated_during_creation_includes_status_message() -> None:
    from chalkcompute._sandbox import SandboxTerminatedError

    client = SandboxClient()
    client._stub = _TerminatedStub()  # type: ignore[assignment]
    try:
        with pytest.raises(SandboxTerminatedError, match="Exit code 0"):
            client.create("python:3.14-slim", wait=True)
    finally:
        client.close()


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

    resolve.assert_called_once_with(
        client._connect, VolumeMount(name="my-data", mount_path="/data")
    )
    assert client.create_kwargs is not None
    assert client.create_kwargs["volumes"] == [
        VolumeMount(name="my-data", mount_path="/data")
    ]


def test_sandbox_accepts_version_pinned_volume_handle() -> None:
    from chalkcompute._volume import Volume, VolumeMount

    volume = Volume("my-data", client=MagicMock()).at_version(7)
    client = _RecordingSandboxClient()
    sandbox = Sandbox(
        image="ubuntu:latest",
        volumes=[volume],
        sandbox_client=client,  # type: ignore[arg-type]
    )

    with patch(
        "chalkcompute._sandbox.referenced_volume_mount",
        side_effect=lambda _connect, mount: mount,
    ):
        sandbox.run()

    assert volume.version_id == 7
    assert client.create_kwargs is not None
    assert client.create_kwargs["volumes"] == [
        VolumeMount(
            name="my-data",
            mount_path="/volumes/my-data",
            version_id=7,
        )
    ]


def test_sandbox_accepts_writable_forked_volume_handle() -> None:
    from chalkcompute._volume import ForkInfo, Volume, VolumeMount

    volume_client = MagicMock()
    volume_client._create_ref.return_value = ForkInfo(name="sandbox-a", version_id=7)
    volume = Volume("my-data", client=volume_client).at_version(7).fork("sandbox-a")
    client = _RecordingSandboxClient()
    sandbox = Sandbox(
        image="ubuntu:latest",
        volumes=[volume],
        sandbox_client=client,  # type: ignore[arg-type]
    )

    with patch(
        "chalkcompute._sandbox.referenced_volume_mount",
        side_effect=lambda _connect, mount: mount,
    ):
        sandbox.run()

    assert volume.version_id is None
    assert client.create_kwargs is not None
    assert client.create_kwargs["volumes"] == [
        VolumeMount(
            name="my-data",
            mount_path="/volumes/my-data",
            ref_name="sandbox-a",
        )
    ]


def test_version_pinned_mount_is_serialized_to_proto() -> None:
    mount = (
        ContainerSpec(
            image="ubuntu:latest",
            volumes=[VolumeMount(name="my-data", mount_path="/data", version_id=7)],
        )
        .to_proto()
        .volumes[0]
    )

    assert mount.version_id == 7
    assert mount.has_field("version_id")


def test_fork_mount_is_serialized_to_proto() -> None:
    mount = (
        ContainerSpec(
            image="ubuntu:latest",
            volumes=[
                VolumeMount(name="my-data", mount_path="/data", ref_name="sandbox-a")
            ],
        )
        .to_proto()
        .volumes[0]
    )

    assert mount.ref_name == "sandbox-a"
    assert mount.has_field("ref_name")


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
