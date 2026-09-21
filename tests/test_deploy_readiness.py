from __future__ import annotations

from typing import Any

import pytest

from protobuf import Oneof
from chalkcompute._image import Image

import chalkcompute._function as function_mod
import chalkcompute._cls as cls_mod
from chalkcompute._cls import RemoteClass, method
from chalkcompute._function import FunctionError, FunctionVersionInfo, RemoteFunction
from chalkcompute._gen.chalk.container.v1 import service_pb as container_pb
from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import service_pb as catalog_pb
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from chalkcompute._scaling_group import ScalingGroup
from chalkcompute._volume import VolumeMount
from tests._fakes import FakeMethod, StubbedConnectClient


class _RemoteFunctionReadinessClient(StubbedConnectClient):
    create_status = "Running"

    def __init__(self) -> None:
        self.methods: list[str] = []
        self.requests: dict[str, Any] = {}
        self._env_id: str | None = None
        self.polls = [
            sg_pb.ScalingGroupResponse(
                status="Pending", ready_replicas=1, available_replicas=1
            ),
            sg_pb.ScalingGroupResponse(
                status="Available", ready_replicas=1, available_replicas=1
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
        self.requests[method.name] = request
        if method.name == "ensure_external_function":
            assert isinstance(request, catalog_pb.EnsureExternalFunctionRequest)
            assert request.spec is not None and request.spec.version_spec is not None
            assert request.spec.version_spec.spec is not None
            assert request.spec.version_spec.spec.container_spec is not None
            return catalog_pb.EnsureExternalFunctionResponse(
                result=Oneof(
                    "complete",
                    catalog_pb.EnsuredExternalFunction(
                        external_function=catalog_pb.ExternalFunction(
                            id="fn-version-1",
                            name=request.function_name,
                            current_version=catalog_pb.ExternalFunctionVersion(
                                id="fn-version-1",
                                function_id="fn-1",
                                function_name=request.function_name,
                                version=1,
                                scaling_group_name=request.spec.version_spec.spec.container_spec.name,
                            ),
                        ),
                    ),
                )
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
        self.requests = {}
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

    monkeypatch.setattr(
        RemoteFunction, "_build_deploy_image", lambda _: Image("image:test")
    )
    monkeypatch.setattr(function_mod.time, "sleep", lambda _: None)

    fn.deploy(poll_interval=0, ready_timeout=10)

    assert client.methods == [
        "ensure_external_function",
        "get_scaling_group",
        "get_scaling_group",
    ]
    request = client.requests["ensure_external_function"]
    probe = request.spec.version_spec.spec.container_spec.startup_probe
    assert probe.check is not None and probe.check.field == "grpc"
    assert isinstance(probe.check.value, container_pb.GrpcProbe)


def test_remote_function_deploy_allows_zero_min_replicas(monkeypatch) -> None:
    def add(x: int) -> int:
        return x

    client = _RemoteFunctionReadinessClient()
    client.polls = [sg_pb.ScalingGroupResponse(status="Running")]
    fn = RemoteFunction(add, name="fn-zero", min_replicas=0)
    fn._client = client  # type: ignore[assignment]

    monkeypatch.setattr(
        RemoteFunction, "_build_deploy_image", lambda _: Image("image:test")
    )

    fn.deploy(poll_interval=0, ready_timeout=10)

    assert client.methods == ["ensure_external_function", "get_scaling_group"]


def test_remote_function_deploy_allows_scaled_to_zero(monkeypatch) -> None:
    def add(x: int) -> int:
        return x

    client = _RemoteFunctionScaledToZeroClient()
    fn = RemoteFunction(add, name="fn-zero", min_replicas=0)
    fn._client = client  # type: ignore[assignment]

    monkeypatch.setattr(
        RemoteFunction, "_build_deploy_image", lambda _: Image("image:test")
    )

    fn.deploy(poll_interval=0, ready_timeout=10)

    assert client.methods == ["ensure_external_function", "get_scaling_group"]


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

    monkeypatch.setattr(
        RemoteClass, "_build_deploy_image", lambda _: (Image("image:test"), [])
    )
    monkeypatch.setattr(cls_mod.time, "sleep", lambda _: None)

    rc.deploy(poll_interval=0, ready_timeout=10)

    assert client.methods == [
        "ensure_external_function",
        "get_scaling_group",
        "get_scaling_group",
    ]
    request = client.requests["ensure_external_function"]
    probe = request.spec.version_spec.spec.container_spec.startup_probe
    assert probe.check is not None and probe.check.field == "grpc"
    assert isinstance(probe.check.value, container_pb.GrpcProbe)


def test_remote_class_deploy_allows_zero_min_replicas(monkeypatch) -> None:
    class Adder:
        @method
        def add(self, x: int) -> int:
            return x

    client = _RemoteClassReadinessClient()
    client.polls = [sg_pb.ScalingGroupResponse(status="Running")]
    rc = RemoteClass(Adder, name="cls-zero", min_replicas=0)
    rc._client = client  # type: ignore[assignment]

    monkeypatch.setattr(
        RemoteClass, "_build_deploy_image", lambda _: (Image("image:test"), [])
    )

    rc.deploy(poll_interval=0, ready_timeout=10)

    assert client.methods == ["ensure_external_function", "get_scaling_group"]


def test_remote_class_deploy_allows_scaled_to_zero(monkeypatch) -> None:
    class Adder:
        @method
        def add(self, x: int) -> int:
            return x

    client = _RemoteClassScaledToZeroClient()
    rc = RemoteClass(Adder, name="cls-zero", min_replicas=0)
    rc._client = client  # type: ignore[assignment]

    monkeypatch.setattr(
        RemoteClass, "_build_deploy_image", lambda _: (Image("image:test"), [])
    )

    rc.deploy(poll_interval=0, ready_timeout=10)

    assert client.methods == ["ensure_external_function", "get_scaling_group"]


def test_remote_class_resolves_one_shared_source_volume(monkeypatch, tmp_path) -> None:
    class Arithmetic:
        @method
        def add(self, x: int) -> int:
            return x + 1

        @method
        def subtract(self, x: int) -> int:
            return x - 1

    source = tmp_path / "source.py"
    source.write_text("answer = 42\n")
    image = Image("image:test").add_local_file(str(source), "/app/source.py")
    client = _RemoteClassReadinessClient()
    rc = RemoteClass(Arithmetic, name="arithmetic")
    rc._client = client  # type: ignore[assignment]
    resolutions = []

    monkeypatch.setattr(RemoteClass, "_build_deploy_image", lambda _: (image, []))
    monkeypatch.setattr(RemoteClass, "_poll_until_ready", lambda *a, **kw: None)

    def resolve(*args, **kwargs):
        resolutions.append(kwargs)
        return [VolumeMount("arithmetic", "/app", version_id=42)]

    monkeypatch.setattr(cls_mod, "resolve_function_volumes", resolve)

    rc.deploy(poll_interval=0, ready_timeout=10)

    assert len(resolutions) == 1
    assert resolutions[0]["volume_name"] == "arithmetic"
    assert client.methods.count("ensure_external_function") == 2


def test_remote_function_deploy_sends_pinned_source_mounts(
    monkeypatch, tmp_path
) -> None:
    def add(x: int) -> int:
        return x

    client = _RemoteFunctionReadinessClient()
    fn = RemoteFunction(add, name="fn-sources")
    fn._client = client
    source = tmp_path / "entry.py"
    source.write_text("answer = 42\n")
    image = Image("image:test").add_local_file(str(source), "/app/entry.py")
    monkeypatch.setattr(RemoteFunction, "_build_deploy_image", lambda _: image)
    monkeypatch.setattr(function_mod.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        function_mod,
        "resolve_function_volumes",
        lambda *a, **kw: [
            VolumeMount(
                name="source",
                mount_path="/app",
                version_id=42,
            )
        ],
    )
    fn.deploy(poll_interval=0, ready_timeout=10)
    request = client.requests["ensure_external_function"]
    assert request.spec.version_spec.volume_commits == []
    assert request.spec.version_spec.spec.container_spec.image == ""
    assert request.spec.image.base_image == "image:test"
    mounts = request.spec.version_spec.spec.container_spec.volumes
    assert len(mounts) == 1
    assert mounts[0].mount_path == "/app"
    assert mounts[0].version_id == 42
    
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
        self.requests = {}
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


@pytest.mark.parametrize("status", ["Failed", "Error", "Unhealthy"])
@pytest.mark.parametrize(
    "details", [None, "", "Traceback:\n  frame\nValueError: invalid"]
)
def test_remote_function_readiness_error_details(monkeypatch, status, details) -> None:
    def add(x: int) -> int:
        return x

    client = _RemoteFunctionReadinessClient()
    client.polls = [
        sg_pb.ScalingGroupResponse(
            status=status,
            status_message="Exit code 1",
            status_details=details,
        )
    ]
    fn = RemoteFunction(add, name="fn-ready")
    fn._client = client  # type: ignore[assignment]
    times = iter([0, 0, 11])
    monkeypatch.setattr(function_mod.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(function_mod.time, "sleep", lambda _: None)

    with pytest.raises(FunctionError) as exc_info:
        fn._poll_until_ready(sg_name="fn-ready", poll_interval=0, timeout=10)

    expected = (
        "Scaling group fn-ready not ready after 10s "
        "(status=Unhealthy, ready=0, available=0): Exit code 1"
        if status == "Unhealthy"
        else f"Scaling group fn-ready entered {status} state: Exit code 1"
    )
    if details:
        expected += "\n" + details
    assert str(exc_info.value) == expected


@pytest.mark.parametrize("resource", ["function", "class", "scaling_group"])
def test_readiness_logs_transitions_instead_of_each_poll(resource, caplog, monkeypatch):
    import logging

    caplog.set_level(logging.INFO, logger="chalkcompute")
    if resource == "scaling_group":
        client = _ScalingGroupReadinessClient()
        handle = ScalingGroup(image="ubuntu:latest", name="fraud")
        handle._scaling_group_id = "fraud"
    elif resource == "class":

        class Fraud:
            @method
            def score(self, x: int) -> int:
                return x

        client = _RemoteFunctionReadinessClient()
        handle = RemoteClass(Fraud)
    else:
        client = _RemoteFunctionReadinessClient()
        handle = RemoteFunction(lambda x: x)
    client.polls = [
        sg_pb.ScalingGroupResponse(status="Pending"),
        sg_pb.ScalingGroupResponse(status="Pending"),
        sg_pb.ScalingGroupResponse(
            status="Pending", status_message="waiting for startup probe"
        ),
        sg_pb.ScalingGroupResponse(
            status="Available", ready_replicas=1, available_replicas=1
        ),
    ]
    handle._client = client
    monkeypatch.setattr(function_mod.time, "sleep", lambda _: None)
    kwargs = {} if resource == "scaling_group" else {"sg_name": "fraud"}
    handle._poll_until_ready(poll_interval=0, timeout=10, **kwargs)
    assert [record.getMessage() for record in caplog.records] == [
        "Scaling group fraud: Pending ready=0 available=0",
        "Scaling group fraud: Pending ready=0 available=0 — waiting for startup probe",
        "Scaling group fraud: Available ready=1 available=1",
    ]


def test_container_readiness_logs_transitions_instead_of_each_poll(
    caplog, capsys, monkeypatch
):
    import logging
    from types import SimpleNamespace
    from chalkcompute._container import Container
    from chalkcompute import _progress

    caplog.set_level(logging.INFO, logger="chalkcompute")
    container = Container(image="ubuntu:latest")
    container._container_id = "fraud"
    container._name = "fraud"
    progress = _progress.DeployProgress()
    progress.add("container:fraud", "container", 'Container "fraud"')
    monkeypatch.setattr(_progress, "get_progress", lambda: progress)
    states = iter(["Pending", "Pending", "Running"])
    monkeypatch.setattr(
        "chalkcompute._container._get_container_info",
        lambda *args: SimpleNamespace(status=next(states)),
    )
    monkeypatch.setattr(function_mod.time, "sleep", lambda _: None)
    container._poll_until_running(poll_interval=0, timeout=10)
    assert [record.getMessage() for record in caplog.records] == [
        "Container fraud: Pending",
        "Container fraud: Running",
    ]
    output = capsys.readouterr().err
    assert output.count("› Pending") == 1
    assert output.count("› Running") == 1
