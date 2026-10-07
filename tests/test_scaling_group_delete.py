from typing import Any, cast
from unittest.mock import MagicMock

import pytest

import chalkcompute._scaling_group as scaling_group_mod
from chalkcompute import ScalingGroup
from chalkcompute._gen.chalk.container.v1 import service_pb as container_pb
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from chalkcompute._volume import VolumeMount
from tests._fakes import FakeConnectClient


@pytest.mark.parametrize("attach", ["from_name", "from_id"])
@pytest.mark.parametrize(
    ("version_id", "ref_name"), [(None, None), (42, None), (None, "branch")]
)
def test_delete_cleans_up_revision_volumes(
    monkeypatch: pytest.MonkeyPatch,
    attach: str,
    version_id: int | None,
    ref_name: str | None,
) -> None:
    volume = container_pb.VolumeMount(
        name="chalkcompute-sg-files-source",
        mount_path="/app",
        version_id=version_id,
        ref_name=ref_name,
    )

    def handler(method: str, request: Any) -> Any:
        if method == "get_scaling_group":
            return sg_pb.GetScalingGroupResponse(
                scaling_group=sg_pb.ScalingGroupResponse(id="group-1", name="api")
            )
        if method == "list_scaling_group_revisions":
            return sg_pb.ListScalingGroupRevisionsResponse(
                revisions=[
                    sg_pb.ScalingGroupRevisionResponse(
                        spec=sg_pb.ScalingGroupSpec(
                            container_spec=container_pb.ChalkContainerSpec(
                                volumes=[volume]
                            )
                        )
                    )
                ]
            )
        assert method == "delete_scaling_group"
        assert request.id == "group-1"
        return sg_pb.DeleteScalingGroupResponse()

    client = FakeConnectClient(handler=handler)
    monkeypatch.setattr(scaling_group_mod, "ConnectClient", lambda **_: client)
    group = getattr(ScalingGroup, attach)("api" if attach == "from_name" else "group-1")
    driver = MagicMock()
    group._volume_driver = driver

    assert group._owned_volumes_from_revisions()[
        (volume.name, ref_name)
    ] == VolumeMount(volume.name, "/app", version_id=version_id, ref_name=ref_name)
    group.delete()

    if version_id is None:
        driver.delete_volume.assert_called_once_with(volume.name)
    else:
        driver.delete_volume.assert_not_called()
    assert client.calls[-1].method == "delete_scaling_group"
    assert group.id is None
    assert group.info is None


def test_attached_group_deletes_all_revision_forks(monkeypatch):
    mounts = [
        container_pb.VolumeMount(
            name="cc-image-shared", mount_path="/app", ref_name=f"cc-workload-{index}"
        )
        for index in range(3)
    ]

    def handler(method, request):
        if method == "get_scaling_group":
            return sg_pb.GetScalingGroupResponse(
                scaling_group=sg_pb.ScalingGroupResponse(id="group-1", name="api")
            )
        if method == "list_scaling_group_revisions":
            return sg_pb.ListScalingGroupRevisionsResponse(
                revisions=[
                    sg_pb.ScalingGroupRevisionResponse(
                        spec=sg_pb.ScalingGroupSpec(
                            container_spec=container_pb.ChalkContainerSpec(
                                volumes=[mount]
                            )
                        )
                    )
                    for mount in mounts
                ]
            )
        assert method == "delete_scaling_group"
        return sg_pb.DeleteScalingGroupResponse()

    client = FakeConnectClient(handler=handler)
    monkeypatch.setattr(scaling_group_mod, "ConnectClient", lambda **_: client)
    group = ScalingGroup.from_id("group-1")
    driver = MagicMock()
    group._volume_driver = driver
    group.delete()

    assert driver._delete_ref.call_count == 3
    for mount in mounts:
        driver._delete_ref.assert_any_call(mount.name, mount.ref_name)
    driver.delete_volume.assert_not_called()


def test_delete_continues_after_invalid_revision_volume(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    group = ScalingGroup(image="ubuntu:latest", name="api")
    group._scaling_group_id = "group-1"
    client = FakeConnectClient(
        handler=lambda method, request: sg_pb.DeleteScalingGroupResponse()
    )
    group._client = cast(Any, client)
    group._owned_volumes = [VolumeMount("chalkcompute-sg-files-local", "/app")]
    driver = MagicMock()
    group._volume_driver = driver

    def invalid_revisions() -> dict[str, VolumeMount]:
        raise ValueError("version_id and ref_name cannot both be set")

    monkeypatch.setattr(group, "_owned_volumes_from_revisions", invalid_revisions)
    group.delete()

    assert client.calls[-1].method == "delete_scaling_group"
    driver.delete_volume.assert_called_once_with("chalkcompute-sg-files-local")
    assert "Could not inspect revision volumes before delete" in caplog.text
    assert group.id is None
