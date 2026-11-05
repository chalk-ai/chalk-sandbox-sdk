"""Test doubles for the ConnectRPC transport seam (``ConnectClient.stub``/``call``).

Production code obtains a stub via ``client.stub(SomeServiceStub)`` and
invokes it via ``client.call(stub.Method, request, ...)``. These fakes
implement that seam so unit tests assert on the *typed protobuf requests* the SDK
builds, without any HTTP client or network.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable

from chalkcompute._gen.chalk.container.v1 import service_pb as container_pb
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb


@dataclass
class FakeCall:
    """One recorded ``ConnectClient.call`` invocation."""

    method: str
    request: Any
    timeout: float


class FakeMethod:
    """A generated client method represented by its Python method name."""

    def __init__(self, name: str) -> None:
        self.name = name


class _FakeStub:
    def __init__(self, stub_cls: type) -> None:
        self._stub_cls = stub_cls

    def __getattr__(self, name: str) -> FakeMethod:
        return FakeMethod(name)


class StubbedConnectClient:
    """Minimal typed transport seam shared by SDK unit-test doubles.

    Generated client construction is intentionally reduced to a method name;
    requests and responses remain protobuf messages throughout each test.
    """

    def stub(self, stub_cls: type) -> _FakeStub:
        return _FakeStub(stub_cls)


def default_container_handler(method: str, request: Any) -> Any:
    """Successful responses for the ContainerService lifecycle."""
    name = request.container.spec.name if method == "run_container" else "network-test"
    running = container_pb.ContainerResponse(
        id="container-123",
        name=name,
        status="Running",
        pod_name="pod-network-test",
    )
    if method == "run_container":
        return container_pb.RunContainerResponse(container=running)
    if method == "get_container":
        return container_pb.GetContainerResponse(container=running)
    if method == "list_containers":
        return container_pb.ListContainersResponse(containers=[running])
    if method == "stop_container":
        return container_pb.StopContainerResponse()
    if method == "exec_command":
        return container_pb.ExecCommandResponse(stdout=b"", stderr=b"", exit_code=0)
    raise AssertionError(f"unexpected method: {method}")


@dataclass
class FakeConnectClient(StubbedConnectClient):
    """Stands in for ``ConnectClient`` behind the stub/call seam.

    ``handler(method_name, request)`` produces responses (or raises
    ``FakeRpcError``); every invocation is recorded in ``calls``.
    """

    handler: Callable[[str, Any], Any] = default_container_handler
    calls: list[FakeCall] = field(default_factory=list)
    _env_id: str | None = None

    def _ensure_auth(self) -> None:
        pass

    def call(
        self,
        method: FakeMethod,
        request: Any,
        *,
        timeout: float = 30.0,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Any:
        self.calls.append(
            FakeCall(method=method.name, request=request, timeout=timeout)
        )
        return self.handler(method.name, request)


class ScalingGroupClient(StubbedConnectClient):
    def __init__(self, *, authentication: str | None = None) -> None:
        self.requests: list[Any] = []
        self._access_token = "test-token"
        self.ensure_auth_calls = 0
        self.authentication = authentication

    def _ensure_auth(self) -> None:
        self.ensure_auth_calls += 1

    def call(
        self,
        method: FakeMethod,
        request: Any,
        **_: Any,
    ) -> Any:
        self.requests.append(request)
        if method.name == "create_scaling_group":
            assert isinstance(request, sg_pb.CreateScalingGroupRequest)
            assert request.spec is not None and request.spec.container_spec is not None
            spec = request.spec
            name = spec.container_spec.name
            response_type = sg_pb.CreateScalingGroupResponse
        elif method.name == "get_scaling_group":
            assert isinstance(request, sg_pb.GetScalingGroupRequest)
            name = request.name or "existing-group"
            spec = sg_pb.ScalingGroupSpec(
                container_spec=container_pb.ChalkContainerSpec(
                    authentication=self.authentication
                )
            )
            response_type = sg_pb.GetScalingGroupResponse
        else:
            raise AssertionError(f"unexpected method: {method.name}")
        return response_type(
            scaling_group=sg_pb.ScalingGroupResponse(
                id="sg-1",
                name=name,
                status="Available",
                web_url="https://example.com",
                ready_replicas=1,
                available_replicas=1,
                spec=spec,
            )
        )


class MemoryWeightVolume:
    """Immutable snapshots and atomic batches for weight stream safety tests."""

    ref = "main"
    version_id = None

    def __init__(self, *, _owner=None, _version=None):
        from types import SimpleNamespace

        self._owner = _owner or self
        if _owner is None:
            self.snapshots = [{}]
            self.commits = []
            self.fail_commit = False
        self._version = _version
        self.version = SimpleNamespace(id=0 if _version is None else _version)
        self.name = "weights-test"

    @property
    def _files(self):
        index = (
            len(self._owner.snapshots) - 1 if self._version is None else self._version
        )
        return self._owner.snapshots[index]

    def reload(self):
        from types import SimpleNamespace

        self.version = SimpleNamespace(id=len(self._owner.snapshots) - 1)
        return self

    def at_version(self, version):
        return MemoryWeightVolume(_owner=self._owner, _version=version)

    def read_file(self, path):
        from chalkcompute._volume import VolumeFileNotFoundError

        try:
            return self._files[path]
        except KeyError as error:
            raise VolumeFileNotFoundError(path) from error

    def listdir(self, prefix="", recursive=True):
        from chalkcompute._volume import FileInfo

        prefix = prefix.rstrip("/")
        return [
            FileInfo(path=path, size=len(data), kind="file")
            for path, data in self._files.items()
            if not prefix or path.startswith(prefix + "/")
        ]

    def _commit(self, files):
        if self._owner.fail_commit:
            raise RuntimeError("injected commit failure")
        state = dict(self._files)
        state.update(files)
        self._owner.snapshots.append(state)
        self._owner.commits.append(dict(files))
        self.reload()

    def put_file(self, path, data):
        self._commit({path: data})

    def remove_file(self, path):
        state = dict(self._files)
        del state[path]
        self._owner.snapshots.append(state)
        self.reload()

    def batch_upload(self, *, max_files_per_commit=200):
        from contextlib import contextmanager
        from pathlib import Path

        volume = self

        class Batch:
            def __init__(self):
                self.files = {}

            def put(self, path, data):
                self.files[path] = data
                if len(self.files) >= max_files_per_commit:
                    self.flush()

            def put_file(self, path, local_path):
                self.put(path, Path(local_path).read_bytes())

            def flush(self):
                if self.files:
                    volume._commit(self.files)
                    self.files = {}

        @contextmanager
        def upload():
            batch = Batch()
            yield batch
            batch.flush()

        return upload()
