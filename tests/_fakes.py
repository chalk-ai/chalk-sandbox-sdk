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

    def _rpc_metadata(self) -> list[tuple[str, str]]:
        return []

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
