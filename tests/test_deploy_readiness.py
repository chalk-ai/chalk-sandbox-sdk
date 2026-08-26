from __future__ import annotations

from typing import Any

import pytest

import chalkcompute._function as function_mod
import chalkcompute._cls as cls_mod
from chalkcompute._cls import RemoteClass, method
from chalkcompute._function import FunctionError, FunctionVersionInfo, RemoteFunction
from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import service_pb as catalog_pb
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from chalkcompute._scaling_group import ScalingGroup
from tests._fakes import FakeMethod, StubbedConnectClient


class _RemoteFunctionReadinessClient(StubbedConnectClient):
    create_status = "Running"

    def __init__(self) -> None:
        self.methods: list[str] = []
        self._env_id: str | None = None
        self.polls = [
            sg_pb.ScalingGroupResponse(status="Running"),
            sg_pb.ScalingGroupResponse(
                status="Running", ready_replicas=1, available_replicas=1
            ),
        ]

    def _ensure_auth(self) -> None:
        pass

    def call(
        self,
        method: FakeMethod,
        request: Any,
        **_: Any,
    ) -> Any:
        self.methods.append(method.name)
        if method.name == "create_external_function":
            assert isinstance(request, catalog_pb.CreateExternalFunctionRequest)
            assert request.spec is not None and request.spec.spec is not None
            assert request.spec.spec.container_spec is not None
            return catalog_pb.CreateExternalFunctionResponse(
                external_function=catalog_pb.ExternalFunction(
                    id="fn-version-1",
                    name=request.function_name,
                    current_version=catalog_pb.ExternalFunctionVersion(
                        id="fn-version-1",
                        function_id="fn-1",
                        function_name=request.function_name,
                        version=1,
                        scaling_group_name=request.spec.spec.container_spec.name,
                    ),
                ),
            )
        if method.name == "get_scaling_group":
            assert isinstance(request, sg_pb.GetScalingGroupRequest)
            sg = self.polls.pop(0)
            sg.name = request.name
            sg.web_url = "http://127.0.0.1"
            return sg_pb.GetScalingGroupResponse(scaling_group=sg)
        raise AssertionError(f"unexpected method: {method.name}")


class _RemoteFunctionScaledToZeroClient(_RemoteFunctionReadinessClient):
    create_status = "Pending"

    def __init__(self) -> None:
        self.methods = []
        self._env_id = None
        self.polls = [sg_pb.ScalingGroupResponse(status="ScaledToZero")]


class _ScalingGroupReadinessClient(StubbedConnectClient):
    create_status = "Running"
    poll_name = "sg-ready"

    def __init__(self) -> None:
        self.methods: list[str] = []
        self.polls = [
            sg_pb.ScalingGroupResponse(status="Running"),
            sg_pb.ScalingGroupResponse(
                status="Running", ready_replicas=1, available_replicas=1
            ),
        ]

    def call(self, method: FakeMethod, request: Any, **_: Any) -> Any:
        self.methods.append(method.name)
        if method.name == "create_scaling_group":
            assert isinstance(request, sg_pb.CreateScalingGroupRequest)
            assert request.spec is not None and request.spec.container_spec is not None
            return sg_pb.CreateScalingGroupResponse(
                scaling_group=sg_pb.ScalingGroupResponse(
                    id="sg-1",
                    name=request.spec.container_spec.name,
                    status=self.create_status,
                    web_url="http://127.0.0.1",
                )
            )
        if method.name == "get_scaling_group":
            assert isinstance(request, sg_pb.GetScalingGroupRequest)
            sg = self.polls.pop(0)
            sg.id = request.id
            sg.name = self.poll_name
            sg.web_url = "http://127.0.0.1"
            return sg_pb.GetScalingGroupResponse(scaling_group=sg)
        raise AssertionError(f"unexpected method: {method.name}")


class _ScalingGroupScaledToZeroClient(_ScalingGroupReadinessClient):
    create_status = "Pending"
    poll_name = "sg-zero"

    def __init__(self) -> None:
        self.methods = []
        self.polls = [sg_pb.ScalingGroupResponse(status="ScaledToZero")]


class _RemoteClassReadinessClient(_RemoteFunctionReadinessClient):
    pass


class _RemoteClassScaledToZeroClient(_RemoteFunctionScaledToZeroClient):
    pass


def test_remote_function_deploy_waits_for_ready_replicas(monkeypatch) -> None:
    def add(x: int) -> int:
        return x

    client = _RemoteFunctionReadinessClient()
    fn = RemoteFunction(add, name="fn-ready")
    fn._client = client  # type: ignore[assignment]

    monkeypatch.setattr(RemoteFunction, "_build_deploy_image", lambda _: object())
    monkeypatch.setattr(
        function_mod,
        "_build_and_upload",
        lambda *_, **__: ("image:test", [], None, []),
    )
    monkeypatch.setattr(function_mod.time, "sleep", lambda _: None)

    fn.deploy(poll_interval=0, ready_timeout=10)

    assert client.methods == [
        "create_external_function",
        "get_scaling_group",
        "get_scaling_group",
    ]


def test_remote_function_deploy_allows_zero_min_replicas(monkeypatch) -> None:
    def add(x: int) -> int:
        return x

    client = _RemoteFunctionReadinessClient()
    fn = RemoteFunction(add, name="fn-zero", min_replicas=0)
    fn._client = client  # type: ignore[assignment]

    monkeypatch.setattr(RemoteFunction, "_build_deploy_image", lambda _: object())
    monkeypatch.setattr(
        function_mod,
        "_build_and_upload",
        lambda *_, **__: ("image:test", [], None, []),
    )

    fn.deploy(poll_interval=0, ready_timeout=10)

    assert client.methods == ["create_external_function", "get_scaling_group"]


def test_remote_function_deploy_allows_scaled_to_zero(monkeypatch) -> None:
    def add(x: int) -> int:
        return x

    client = _RemoteFunctionScaledToZeroClient()
    fn = RemoteFunction(add, name="fn-zero", min_replicas=0)
    fn._client = client  # type: ignore[assignment]

    monkeypatch.setattr(RemoteFunction, "_build_deploy_image", lambda _: object())
    monkeypatch.setattr(
        function_mod,
        "_build_and_upload",
        lambda *_, **__: ("image:test", [], None, []),
    )

    fn.deploy(poll_interval=0, ready_timeout=10)

    assert client.methods == ["create_external_function", "get_scaling_group"]


def test_scaling_group_deploy_waits_for_ready_replicas(monkeypatch) -> None:
    client = _ScalingGroupReadinessClient()
    sg = ScalingGroup(image="ubuntu:latest", name="sg-ready")
    sg._client = client  # type: ignore[assignment]

    monkeypatch.setattr("chalkcompute._scaling_group.time.sleep", lambda _: None)

    sg.deploy(poll_interval=0, ready_timeout=10)

    assert client.methods == [
        "create_scaling_group",
        "get_scaling_group",
        "get_scaling_group",
    ]


def test_scaling_group_deploy_allows_zero_min_replicas(monkeypatch) -> None:
    client = _ScalingGroupReadinessClient()
    sg = ScalingGroup(image="ubuntu:latest", name="sg-zero", min_replicas=0)
    sg._client = client  # type: ignore[assignment]

    sg.deploy(poll_interval=0, ready_timeout=10)

    assert client.methods == ["create_scaling_group"]


def test_scaling_group_deploy_allows_scaled_to_zero(monkeypatch) -> None:
    client = _ScalingGroupScaledToZeroClient()
    sg = ScalingGroup(image="ubuntu:latest", name="sg-zero", min_replicas=0)
    sg._client = client  # type: ignore[assignment]

    sg.deploy(poll_interval=0, ready_timeout=10)

    assert client.methods == ["create_scaling_group", "get_scaling_group"]


def test_remote_class_deploy_waits_for_ready_replicas(monkeypatch) -> None:
    class Adder:
        @method
        def add(self, x: int) -> int:
            return x

    client = _RemoteClassReadinessClient()
    rc = RemoteClass(Adder, name="cls-ready")
    rc._client = client  # type: ignore[assignment]

    monkeypatch.setattr(RemoteClass, "_build_deploy_image", lambda _: (object(), []))
    monkeypatch.setattr(
        cls_mod,
        "_build_and_upload",
        lambda *_, **__: ("image:test", [], None, []),
    )
    monkeypatch.setattr(cls_mod.time, "sleep", lambda _: None)

    rc.deploy(poll_interval=0, ready_timeout=10)

    assert client.methods == [
        "create_external_function",
        "get_scaling_group",
        "get_scaling_group",
    ]


def test_remote_class_deploy_allows_zero_min_replicas(monkeypatch) -> None:
    class Adder:
        @method
        def add(self, x: int) -> int:
            return x

    client = _RemoteClassReadinessClient()
    rc = RemoteClass(Adder, name="cls-zero", min_replicas=0)
    rc._client = client  # type: ignore[assignment]

    monkeypatch.setattr(RemoteClass, "_build_deploy_image", lambda _: (object(), []))
    monkeypatch.setattr(
        cls_mod,
        "_build_and_upload",
        lambda *_, **__: ("image:test", [], None, []),
    )

    rc.deploy(poll_interval=0, ready_timeout=10)

    assert client.methods == ["create_external_function", "get_scaling_group"]


def test_remote_class_deploy_allows_scaled_to_zero(monkeypatch) -> None:
    class Adder:
        @method
        def add(self, x: int) -> int:
            return x

    client = _RemoteClassScaledToZeroClient()
    rc = RemoteClass(Adder, name="cls-zero", min_replicas=0)
    rc._client = client  # type: ignore[assignment]

    monkeypatch.setattr(RemoteClass, "_build_deploy_image", lambda _: (object(), []))
    monkeypatch.setattr(
        cls_mod,
        "_build_and_upload",
        lambda *_, **__: ("image:test", [], None, []),
    )

    rc.deploy(poll_interval=0, ready_timeout=10)

    assert client.methods == ["create_external_function", "get_scaling_group"]


class _RequestCapturingClient(_RemoteFunctionReadinessClient):
    def __init__(self) -> None:
        super().__init__()
        self.requests: dict[str, Any] = {}

    def call(
        self,
        method: FakeMethod,
        request: Any,
        **kwargs: Any,
    ) -> Any:
        self.requests[method.name] = request
        return super().call(method, request, **kwargs)


def test_remote_function_deploy_forwards_volume_commits(monkeypatch) -> None:
    from chalkcompute._volume import VolumeMount

    def add(x: int) -> int:
        return x

    client = _RequestCapturingClient()
    fn = RemoteFunction(add, name="fn-commits")
    fn._client = client  # type: ignore[assignment]

    from chalkcompute._gen.chalk.volume.v2 import volume_pb

    intent = volume_pb.CommitIntent(
        volume=volume_pb.VolumeRef(name="fn-files-abc"),
        author="chalk:env:agent:a",
    )
    mount = VolumeMount(name="fn-files-abc", mount_path="/app")
    monkeypatch.setattr(RemoteFunction, "_build_deploy_image", lambda _: object())
    monkeypatch.setattr(
        function_mod,
        "_build_and_upload",
        lambda *_, **__: ("image:test", [mount], None, [intent]),
    )
    monkeypatch.setattr(function_mod.time, "sleep", lambda _: None)

    fn.deploy(poll_interval=0, ready_timeout=10)

    request = client.requests["create_external_function"]
    assert isinstance(request, catalog_pb.CreateExternalFunctionRequest)
    assert request.spec is not None
    assert request.spec.volume_commits == [intent]
    assert request.spec.spec is not None
    assert request.spec.spec.container_spec is not None
    assert any(
        m.name == "fn-files-abc" for m in request.spec.spec.container_spec.volumes
    )


class _RemoteFunctionUnhealthyClient(_RemoteFunctionReadinessClient):
    """A scaling group stuck Unhealthy (e.g. CrashLoopBackOff) forever."""

    def __init__(self) -> None:
        self.methods = []
        self._env_id = None
        self.polls = [
            sg_pb.ScalingGroupResponse(
                status="Unhealthy",
                status_message="1/1 replicas unhealthy: Error (exit code 1)",
            )
        ]


def test_remote_function_deploy_fails_fast_on_unhealthy_scaling_group(
    monkeypatch,
) -> None:
    def add(x: int) -> int:
        return x

    client = _RemoteFunctionUnhealthyClient()
    fn = RemoteFunction(add, name="fn-unhealthy")
    fn._client = client  # type: ignore[assignment]

    monkeypatch.setattr(RemoteFunction, "_build_deploy_image", lambda _: object())
    monkeypatch.setattr(
        function_mod,
        "_build_and_upload",
        lambda *_, **__: ("image:test", [], None, []),
    )
    monkeypatch.setattr(function_mod.time, "sleep", lambda _: None)

    with pytest.raises(FunctionError, match="Unhealthy.*exit code 1"):
        fn.deploy(poll_interval=0, ready_timeout=10)

    # Failed on the first poll rather than looping until ready_timeout.
    assert client.methods == ["create_external_function", "get_scaling_group"]


class _WaitReadyUnhealthyClient(StubbedConnectClient):
    def __init__(self) -> None:
        self.get_scaling_group_calls = 0

    def call(self, method: FakeMethod, request: Any, **_: Any) -> Any:
        assert method.name == "get_scaling_group"
        self.get_scaling_group_calls += 1
        return sg_pb.GetScalingGroupResponse(
            scaling_group=sg_pb.ScalingGroupResponse(
                name=request.name,
                status="Unhealthy",
                status_message="1/1 replicas unhealthy: Error (exit code 1)",
            )
        )


def test_wait_ready_fails_fast_on_unhealthy_scaling_group() -> None:
    def add(x: int) -> int:
        return x

    client = _WaitReadyUnhealthyClient()
    fn = RemoteFunction(add, name="fn-unhealthy")
    fn._client = client  # type: ignore[assignment]
    fn._version_info = FunctionVersionInfo(
        id="version-1",
        function_name="fn-unhealthy",
        version=1,
        scaling_group_name="sg-unhealthy",
    )

    with pytest.raises(FunctionError, match="Unhealthy.*exit code 1"):
        fn.wait_ready(timeout=10, poll_interval=0)

    # Failed on the first poll rather than looping until the 10s timeout.
    assert client.get_scaling_group_calls == 1
