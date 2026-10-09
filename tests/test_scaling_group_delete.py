from unittest.mock import MagicMock, call

import pytest

import chalkcompute._scaling_group as scaling_group_mod
from chalkcompute import Image, ScalingGroup, VolumeMount
from chalkcompute._gen.chalk.container.v1 import service_pb as container_pb
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from tests._fakes import FakeConnectClient


def revision(*volumes):
    return sg_pb.ScalingGroupRevisionResponse(
        spec=sg_pb.ScalingGroupSpec(
            container_spec=container_pb.ChalkContainerSpec(volumes=list(volumes))
        )
    )


@pytest.mark.parametrize("attach", [False, True])
@pytest.mark.parametrize("local_kind", ["file", "dir"])
def test_delete_cleans_up_lazy_file_revision_volumes(
    monkeypatch, tmp_path, attach, local_kind
):
    current = VolumeMount("chalkcompute-sg-files-current", "/app", 7)
    historical = "chalkcompute-sg-files-old"
    driver = MagicMock()
    driver.upload_lazy_files.return_value = [current]
    group = sg_pb.ScalingGroupResponse(
        id="sg-1",
        name="test-group",
        status="Available",
        ready_replicas=1,
        available_replicas=1,
    )

    def handler(method, request):
        if method == "create_scaling_group":
            group.spec = request.spec
            return sg_pb.CreateScalingGroupResponse(scaling_group=group)
        if method == "get_scaling_group":
            return sg_pb.GetScalingGroupResponse(scaling_group=group)
        if method == "list_scaling_group_revisions":
            assert request.include_deleted
            assert request.scaling_group_key.value == "sg-1"
            if not request.cursor:
                return sg_pb.ListScalingGroupRevisionsResponse(
                    revisions=[revision(*group.spec.container_spec.volumes)],
                    next_cursor="page-2",
                )
            assert request.cursor == "page-2"
            return sg_pb.ListScalingGroupRevisionsResponse(
                revisions=[
                    revision(
                        container_pb.VolumeMount(name=historical, mount_path="/app"),
                        container_pb.VolumeMount(
                            name=current.name, mount_path="/app", version_id=7
                        ),
                        container_pb.VolumeMount(name="user-data", mount_path="/data"),
                    )
                ]
            )
        if method == "delete_scaling_group":
            assert request.id == "sg-1"
            return sg_pb.DeleteScalingGroupResponse()
        raise AssertionError(f"unexpected method: {method}")

    client = FakeConnectClient(handler=handler)
    monkeypatch.setattr(scaling_group_mod, "ConnectClient", lambda **_: client)
    monkeypatch.setattr(scaling_group_mod, "build_image", lambda *_, **__: "built:1")
    monkeypatch.setattr(
        scaling_group_mod.VolumeClient, "from_connect", lambda _: driver
    )
    (tmp_path / "app.py").write_text("print('hello')")
    image = Image.debian_slim()
    if local_kind == "file":
        image = image.add_local_file(str(tmp_path / "app.py"), "/app/app.py")
    else:
        image = image.add_local_dir(str(tmp_path), "/app")
    sg = ScalingGroup(image=image, name="test-group").deploy()
    driver.upload_lazy_files.assert_called_once()
    if attach:
        sg = ScalingGroup.from_name("test-group")

    sg.delete()

    assert client.calls[-1].method == "delete_scaling_group"
    assert driver.delete_volume.call_args_list == [call(current.name), call(historical)]
    assert sg._scaling_group_id is None
    assert sg._scaling_group_info is None
    assert sg._owned_volumes == []
    assert sg._volumes == []


@pytest.mark.parametrize(
    "selector, expected_version, expected_ref",
    [
        ({}, None, None),
        ({"version_id": 7}, 7, None),
        ({"version_id": 0}, 0, None),
        ({"ref_name": "released"}, None, "released"),
    ],
)
def test_revision_volume_selectors(selector, expected_version, expected_ref):
    name = "chalkcompute-sg-files-test"
    client = FakeConnectClient(
        handler=lambda *_: sg_pb.ListScalingGroupRevisionsResponse(
            revisions=[
                revision(
                    container_pb.VolumeMount(name=name, mount_path="/app", **selector)
                )
            ]
        )
    )
    sg = ScalingGroup(image="nginx:alpine")
    sg._client = client
    sg._scaling_group_id = "sg-1"

    volumes = sg._owned_volumes_from_revisions()

    assert volumes[name] == VolumeMount(name, "/app", expected_version, expected_ref)


def test_revision_inspection_failure_does_not_block_delete(caplog):
    current = VolumeMount("chalkcompute-sg-files-current", "/app", 7)
    driver = MagicMock()

    def handler(method, request):
        if method == "list_scaling_group_revisions":
            raise ValueError("invalid revision")
        assert method == "delete_scaling_group"
        assert request.id == "sg-1"
        return sg_pb.DeleteScalingGroupResponse()

    client = FakeConnectClient(handler=handler)
    sg = ScalingGroup(image="nginx:alpine")
    sg._client = client
    sg._scaling_group_id = "sg-1"
    sg._owned_volumes = [current]
    sg._volume_driver = driver

    sg.delete()

    assert client.calls[-1].method == "delete_scaling_group"
    driver.delete_volume.assert_called_once_with(current.name)
    assert sg._scaling_group_id is None
    assert (
        "Could not inspect revision volumes before delete: invalid revision"
        in caplog.text
    )
