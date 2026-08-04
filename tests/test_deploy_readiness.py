from __future__ import annotations

from typing import Any

import chalkcompute._function as function_mod
import chalkcompute._cls as cls_mod
from chalkcompute._cls import RemoteClass, method
from chalkcompute._function import RemoteFunction
from chalkcompute._scaling_group import ScalingGroup


class _Response:
    status_code = 200
    text = ""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def json(self) -> dict[str, Any]:
        return self._data


class _RemoteFunctionReadinessClient:
    def __init__(self) -> None:
        self.methods: list[str] = []
        self._env_id: str | None = None
        self.polls = [
            {"status": "Running", "readyReplicas": 0, "availableReplicas": 0},
            {"status": "Running", "readyReplicas": 1, "availableReplicas": 1},
        ]

    def _ensure_auth(self) -> None:
        pass

    def rpc(
        self,
        _service: str,
        method: str,
        request: dict[str, Any],
        **_: Any,
    ) -> _Response:
        self.methods.append(method)
        if method == "CreateExternalFunctionVersion":
            return _Response(
                {
                    "externalFunctionVersion": {
                        "id": "fn-version-1",
                        "functionName": request["functionName"],
                        "version": 1,
                        "scalingGroupName": request["spec"]["containerSpec"]["name"],
                    },
                    "scalingGroup": {
                        "status": "Running",
                        "webUrl": "http://127.0.0.1",
                        "port": 6666,
                        "readyReplicas": 0,
                        "availableReplicas": 0,
                    },
                }
            )
        if method == "GetScalingGroup":
            sg = self.polls.pop(0)
            return _Response(
                {
                    "scalingGroup": {
                        "name": request["name"],
                        "webUrl": "http://127.0.0.1",
                        **sg,
                    }
                }
            )
        raise AssertionError(f"unexpected method: {method}")


class _RemoteFunctionScaledToZeroClient(_RemoteFunctionReadinessClient):
    def __init__(self) -> None:
        self.methods: list[str] = []
        self._env_id: str | None = None
        self.polls = [
            {"status": "ScaledToZero", "readyReplicas": None, "availableReplicas": None}
        ]

    def rpc(
        self,
        _service: str,
        method: str,
        request: dict[str, Any],
        **_: Any,
    ) -> _Response:
        self.methods.append(method)
        if method == "CreateExternalFunctionVersion":
            return _Response(
                {
                    "externalFunctionVersion": {
                        "id": "fn-version-1",
                        "functionName": request["functionName"],
                        "version": 1,
                        "scalingGroupName": request["spec"]["containerSpec"]["name"],
                    },
                    "scalingGroup": {
                        "status": "Pending",
                        "webUrl": "http://127.0.0.1",
                        "port": 6666,
                    },
                }
            )
        if method == "GetScalingGroup":
            sg = self.polls.pop(0)
            return _Response(
                {
                    "scalingGroup": {
                        "name": request["name"],
                        "webUrl": "http://127.0.0.1",
                        **sg,
                    }
                }
            )
        raise AssertionError(f"unexpected method: {method}")


class _ScalingGroupReadinessClient:
    def __init__(self) -> None:
        self.methods: list[str] = []
        self.polls = [
            {"status": "Running", "readyReplicas": 0, "availableReplicas": 0},
            {"status": "Running", "readyReplicas": 1, "availableReplicas": 1},
        ]

    def rpc(
        self,
        _service: str,
        method: str,
        request: dict[str, Any],
        **_: Any,
    ) -> _Response:
        self.methods.append(method)
        if method == "CreateScalingGroup":
            return _Response(
                {
                    "scalingGroup": {
                        "id": "sg-1",
                        "name": request["spec"]["containerSpec"]["name"],
                        "status": "Running",
                        "webUrl": "http://127.0.0.1",
                        "readyReplicas": 0,
                        "availableReplicas": 0,
                    }
                }
            )
        if method == "GetScalingGroup":
            sg = self.polls.pop(0)
            return _Response(
                {
                    "scalingGroup": {
                        "id": request["id"],
                        "name": "sg-ready",
                        "webUrl": "http://127.0.0.1",
                        **sg,
                    }
                }
            )
        raise AssertionError(f"unexpected method: {method}")


class _ScalingGroupScaledToZeroClient(_ScalingGroupReadinessClient):
    def __init__(self) -> None:
        self.methods: list[str] = []
        self.polls = [
            {"status": "ScaledToZero", "readyReplicas": None, "availableReplicas": None}
        ]

    def rpc(
        self,
        _service: str,
        method: str,
        request: dict[str, Any],
        **_: Any,
    ) -> _Response:
        self.methods.append(method)
        if method == "CreateScalingGroup":
            return _Response(
                {
                    "scalingGroup": {
                        "id": "sg-1",
                        "name": request["spec"]["containerSpec"]["name"],
                        "status": "Pending",
                        "webUrl": "http://127.0.0.1",
                    }
                }
            )
        if method == "GetScalingGroup":
            sg = self.polls.pop(0)
            return _Response(
                {
                    "scalingGroup": {
                        "id": request["id"],
                        "name": "sg-zero",
                        "webUrl": "http://127.0.0.1",
                        **sg,
                    }
                }
            )
        raise AssertionError(f"unexpected method: {method}")


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
        "CreateExternalFunctionVersion",
        "GetScalingGroup",
        "GetScalingGroup",
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

    assert client.methods == ["CreateExternalFunctionVersion"]


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

    assert client.methods == ["CreateExternalFunctionVersion", "GetScalingGroup"]


def test_scaling_group_deploy_waits_for_ready_replicas(monkeypatch) -> None:
    client = _ScalingGroupReadinessClient()
    sg = ScalingGroup(image="ubuntu:latest", name="sg-ready")
    sg._client = client  # type: ignore[assignment]

    monkeypatch.setattr("chalkcompute._scaling_group.time.sleep", lambda _: None)

    sg.deploy(poll_interval=0, ready_timeout=10)

    assert client.methods == [
        "CreateScalingGroup",
        "GetScalingGroup",
        "GetScalingGroup",
    ]


def test_scaling_group_deploy_allows_zero_min_replicas(monkeypatch) -> None:
    client = _ScalingGroupReadinessClient()
    sg = ScalingGroup(image="ubuntu:latest", name="sg-zero", min_replicas=0)
    sg._client = client  # type: ignore[assignment]

    sg.deploy(poll_interval=0, ready_timeout=10)

    assert client.methods == ["CreateScalingGroup"]


def test_scaling_group_deploy_allows_scaled_to_zero(monkeypatch) -> None:
    client = _ScalingGroupScaledToZeroClient()
    sg = ScalingGroup(image="ubuntu:latest", name="sg-zero", min_replicas=0)
    sg._client = client  # type: ignore[assignment]

    sg.deploy(poll_interval=0, ready_timeout=10)

    assert client.methods == ["CreateScalingGroup", "GetScalingGroup"]


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
        "CreateExternalFunctionVersion",
        "GetScalingGroup",
        "GetScalingGroup",
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

    assert client.methods == ["CreateExternalFunctionVersion"]


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

    assert client.methods == ["CreateExternalFunctionVersion", "GetScalingGroup"]


class _RequestCapturingClient(_RemoteFunctionReadinessClient):
    def __init__(self) -> None:
        super().__init__()
        self.requests: dict[str, dict[str, Any]] = {}

    def rpc(
        self,
        service: str,
        method: str,
        request: dict[str, Any],
        **kwargs: Any,
    ) -> _Response:
        self.requests[method] = request
        return super().rpc(service, method, request, **kwargs)


def test_remote_function_deploy_forwards_volume_commits(monkeypatch) -> None:
    from chalkcompute._volume import VolumeMount

    def add(x: int) -> int:
        return x

    client = _RequestCapturingClient()
    fn = RemoteFunction(add, name="fn-commits")
    fn._client = client  # type: ignore[assignment]

    intent = {"volume": {"name": "fn-files-abc"}, "author": "chalk:env:agent:a"}
    mount = VolumeMount(
        name="fn-files-abc", mount_path="/app", type="versioned_chalkfs"
    )
    monkeypatch.setattr(RemoteFunction, "_build_deploy_image", lambda _: object())
    monkeypatch.setattr(
        function_mod,
        "_build_and_upload",
        lambda *_, **__: ("image:test", [mount], None, [intent]),
    )
    monkeypatch.setattr(function_mod.time, "sleep", lambda _: None)

    fn.deploy(poll_interval=0, ready_timeout=10)

    request = client.requests["CreateExternalFunctionVersion"]
    assert request["volumeCommits"] == [intent]
    mounts = request["spec"]["containerSpec"]["volumes"]
    assert any(m["name"] == "fn-files-abc" for m in mounts)
