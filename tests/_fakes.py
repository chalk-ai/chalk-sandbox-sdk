"""Test doubles for the gRPC transport seam (``ConnectClient.stub``/``call``).

Production code obtains a stub via ``client.stub(SomeServiceStub)`` and
invokes it via ``client.call(stub.Method, request, ...)``. These fakes
implement that seam so unit tests assert on the *typed pb2 requests* the SDK
builds, without any channel or network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from chalkcompute._gen.chalk.container.v1 import service_pb2 as container_pb


@dataclass
class FakeCall:
    """One recorded ``ConnectClient.call`` invocation."""

    method: str
    request: Any
    timeout: float


class _FakeMethod:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeStub:
    def __init__(self, stub_cls: type) -> None:
        self._stub_cls = stub_cls

    def __getattr__(self, name: str) -> _FakeMethod:
        return _FakeMethod(name)


def default_container_handler(method: str, request: Any) -> Any:
    """Successful responses for the ContainerService lifecycle."""
    name = request.container.spec.name if method == "RunContainer" else "network-test"
    running = container_pb.ContainerResponse(
        id="container-123",
        name=name,
        status="Running",
        pod_name="pod-network-test",
    )
    if method == "RunContainer":
        return container_pb.RunContainerResponse(container=running)
    if method == "GetContainer":
        return container_pb.GetContainerResponse(container=running)
    if method == "ListContainers":
        return container_pb.ListContainersResponse(containers=[running])
    if method == "StopContainer":
        return container_pb.StopContainerResponse()
    if method == "ExecCommand":
        return container_pb.ExecCommandResponse(stdout=b"", stderr=b"", exit_code=0)
    raise AssertionError(f"unexpected method: {method}")


@dataclass
class FakeConnectClient:
    """Stands in for ``ConnectClient`` behind the stub/call seam.

    ``handler(method_name, request)`` produces responses (or raises
    ``FakeRpcError``); every invocation is recorded in ``calls``.
    """

    handler: Callable[[str, Any], Any] = default_container_handler
    calls: list[FakeCall] = field(default_factory=list)
    _env_id: str | None = None

    def _ensure_auth(self) -> None:
        pass

    def stub(self, stub_cls: type) -> _FakeStub:
        return _FakeStub(stub_cls)

    def call(
        self,
        method: _FakeMethod,
        request: Any,
        *,
        timeout: float = 30.0,
    ) -> Any:
        self.calls.append(
            FakeCall(method=method.name, request=request, timeout=timeout)
        )
        return self.handler(method.name, request)
