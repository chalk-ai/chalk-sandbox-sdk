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
