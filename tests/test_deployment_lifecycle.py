from __future__ import annotations

from typing import Any

import chalkcompute._function as function_mod
from chalkcompute import ScaleFromZeroRequestPolicy
from chalkcompute._function import RemoteFunction
from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import service_pb as catalog_pb
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from chalkcompute._scaling_group import ScalingGroup
from tests._fakes import FakeMethod, StubbedConnectClient


def _version(number: int) -> catalog_pb.ExternalFunctionVersion:
    return catalog_pb.ExternalFunctionVersion(
        id=f"version-{number}",
        function_id="function-1",
        function_name="add",
        version=number,
        scaling_group_name="add",
        scaling_group_revision_id=f"revision-{number}",
    )


def _function(number: int) -> catalog_pb.ExternalFunction:
    return catalog_pb.ExternalFunction(
        id="function-1",
        name="add",
        current_version=_version(number),
    )


class _ScalingLifecycleClient(StubbedConnectClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def call(self, method: FakeMethod, request: Any, **_: Any) -> Any:
        self.calls.append((method.name, request))
        if method.name == "create_scaling_group":
            return sg_pb.CreateScalingGroupResponse(
                scaling_group=sg_pb.ScalingGroupResponse(
                    id="group-1",
                    name="api",
                    revision_id="revision-1",
                    status="Running",
                    ready_replicas=1,
                    available_replicas=1,
                )
            )
        if method.name == "update_scaling_group":
            revision_id = "revision-2" if request.spec is not None else "revision-1"
            return sg_pb.UpdateScalingGroupResponse(
                scaling_group=sg_pb.ScalingGroupResponse(
                    id="group-1",
                    name="api",
                    revision_id=revision_id,
                    status="Running",
                    ready_replicas=1,
                    available_replicas=1,
                )
            )
        if method.name == "list_scaling_group_revisions":
            return sg_pb.ListScalingGroupRevisionsResponse(
                revisions=[
                    sg_pb.ScalingGroupRevisionResponse(
                        id="revision-2",
                        scaling_group_id="group-1",
                        scaling_group_name="api",
                        status="Running",
                        latest=True,
                    ),
                    sg_pb.ScalingGroupRevisionResponse(
                        id="revision-1",
                        scaling_group_id="group-1",
                        scaling_group_name="api",
                        status="Superseded",
                    ),
                ]
            )
        raise AssertionError(f"unexpected method: {method.name}")


class _FunctionLifecycleClient(StubbedConnectClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._env_id: str | None = None

    def call(self, method: FakeMethod, request: Any, **_: Any) -> Any:
        self.calls.append((method.name, request))
        if method.name == "create_external_function":
            return catalog_pb.CreateExternalFunctionResponse(
                external_function=_function(1)
            )
        if method.name == "update_external_function":
            selected = 2 if request.spec is not None else 1
            return catalog_pb.UpdateExternalFunctionResponse(
                external_function=_function(selected)
            )
        if method.name == "get_external_function":
            return catalog_pb.GetExternalFunctionResponse(
                external_function=_function(1),
                scaling_group=sg_pb.ScalingGroupResponse(
                    id="group-1",
                    name="add",
                    revision_id="revision-1",
                    status="Running",
                    ready_replicas=1,
                    available_replicas=1,
                    web_url="https://add.example.com",
                ),
            )
        if method.name == "list_external_function_versions":
            return catalog_pb.ListExternalFunctionVersionsResponse(
                entries=[
                    catalog_pb.ListExternalFunctionVersionsEntry(
                        external_function_version=_version(2)
                    ),
                    catalog_pb.ListExternalFunctionVersionsEntry(
                        external_function_version=_version(1)
                    ),
                ]
            )
        if method.name == "delete_external_function":
            return catalog_pb.DeleteExternalFunctionResponse()
        raise AssertionError(f"unexpected method: {method.name}")


def test_scaling_group_redeploy_history_and_rollback() -> None:
    client = _ScalingLifecycleClient()
    group = ScalingGroup(image="ubuntu:latest", name="api")
    group._client = client  # type: ignore[assignment]

    group.deploy().deploy()

    assert group.id == "group-1"
    assert group.current_revision_id == "revision-2"
    update = next(
        request for method, request in client.calls if method == "update_scaling_group"
    )
    assert update.update_mask is not None
    assert update.update_mask.paths == ["spec"]

    revisions = group.revisions()
    assert [revision.id for revision in revisions] == ["revision-2", "revision-1"]
    assert revisions[0].is_current is True

    group.rollback("revision-1", wait_ready=False)

    rollback = [
        request for method, request in client.calls if method == "update_scaling_group"
    ][-1]
    assert rollback.update_mask is not None
    assert rollback.update_mask.paths == ["traffic"]
    assert rollback.traffic is not None
    assert rollback.traffic.targets[0].percent == 100
    assert rollback.traffic.targets[0].revision_target.value == "revision-1"
    assert group.current_revision_id == "revision-1"


def test_scaling_group_scale_from_zero_request_policy() -> None:
    for group, expected in (
        (
            ScalingGroup(image="ubuntu:latest", name="default-policy"),
            sg_pb.ScaleFromZeroRequestPolicy.UNSPECIFIED,
        ),
        (
            ScalingGroup(
                image="ubuntu:latest",
                name="hold-policy",
                scale_from_zero_request_policy=ScaleFromZeroRequestPolicy.HOLD,
            ),
            sg_pb.ScaleFromZeroRequestPolicy.HOLD,
        ),
    ):
        client = _ScalingLifecycleClient()
        group._client = client  # type: ignore[assignment]

        group.deploy()

        request = client.calls[0][1]
        assert request.spec is not None
        assert request.spec.scaling_spec is not None
        assert request.spec.scaling_spec.scale_from_zero_request_policy == expected


def test_function_redeploy_history_rollback_refresh_and_delete(monkeypatch) -> None:
    def add(x: int) -> int:
        return x + 1

    client = _FunctionLifecycleClient()
    function = RemoteFunction(add, name="add")
    function._client = client  # type: ignore[assignment]
    monkeypatch.setattr(RemoteFunction, "_build_deploy_image", lambda _: object())
    monkeypatch.setattr(
        function_mod,
        "_build_and_upload",
        lambda *_, **__: ("image:test", [], None, []),
    )
    monkeypatch.setattr(RemoteFunction, "_poll_until_ready", lambda *_, **__: None)

    function.deploy().deploy()

    assert function.function_id == "function-1"
    assert function.version_info is not None
    assert function.version_info.id == "version-2"
    update = next(
        request
        for method, request in client.calls
        if method == "update_external_function"
    )
    assert update.update_mask is not None
    assert update.update_mask.paths == ["spec"]

    versions = function.versions()
    assert [version.id for version in versions] == ["version-2", "version-1"]
    assert versions[0].is_current is True

    function.rollback("version-1", wait_ready=False)

    rollback = [
        request
        for method, request in client.calls
        if method == "update_external_function"
    ][-1]
    assert rollback.update_mask is not None
    assert rollback.update_mask.paths == ["traffic"]
    assert rollback.traffic is not None
    assert rollback.traffic.targets[0].percent == 100
    assert rollback.traffic.targets[0].version_target.value == "version-1"

    refreshed = function.refresh()
    assert refreshed.id == "version-1"
    assert function.web_url == "https://add.example.com"

    function.delete()
    assert function.function_id is None
    assert client.calls[-1][0] == "delete_external_function"


def test_from_name_uses_selected_parent_version(monkeypatch) -> None:
    client = _FunctionLifecycleClient()
    monkeypatch.setattr(
        function_mod,
        "ConnectClient",
        lambda chalk_client=None: client,
    )

    function = RemoteFunction.from_name("add")

    assert function.function_id == "function-1"
    assert function.version_info is not None
    assert function.version_info.id == "version-1"
    assert client.calls[0][0] == "get_external_function"
