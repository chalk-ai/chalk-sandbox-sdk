"""Tests for the typed-volume single-source-of-truth model.

The mount type is a property of the volume (:class:`VolumeMount.type`), not of
the request being built or of a transient driver. These tests pin that
behavior so a deploy that emits more than one create/update request for the
same scaling group always stamps the *same* type onto the same volume name,
which is what stops the backend ChalkScalingGroup CR from flip-flopping
between ``chalkfs`` and ``versioned_chalkfs``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import grpc
import pytest

from chalkcompute._function import _make_container_spec
from chalkcompute._volume import (
    VolumeClient,
    VolumeError,
    VolumeMount,
    resolve_referenced_volume_type,
)
from chalkcompute._vvolume import VersionedVolumeClient


class _FakeRpcError(grpc.RpcError):
    def __init__(self, code: grpc.StatusCode) -> None:
        self._code = code

    def code(self) -> grpc.StatusCode:
        return self._code


def _spec(volumes: list[VolumeMount]) -> dict:
    return _make_container_spec(
        name="sg",
        image_uri="img:latest",
        entrypoint=["run"],
        env={},
        cpu=None,
        memory=None,
        gpu=None,
        volumes=volumes,
    )


def test_each_volume_keeps_its_own_type() -> None:
    # A base (versioned) volume and an uploaded (non-versioned) volume must each
    # keep their own type rather than all collapsing to a single type.
    volumes = [
        VolumeMount(name="base", mount_path="/base", type="versioned_chalkfs"),
        VolumeMount(name="uploaded", mount_path="/files", type="chalkfs"),
    ]
    spec = _spec(volumes)
    assert spec["volumes"] == [
        {"name": "base", "mountPath": "/base", "type": "versioned_chalkfs"},
        {"name": "uploaded", "mountPath": "/files", "type": "chalkfs"},
    ]


def test_make_container_spec_has_no_mount_type_param() -> None:
    import inspect

    params = inspect.signature(_make_container_spec).parameters
    assert "mount_type" not in params


def test_repeated_spec_builds_are_stable() -> None:
    # The class deploy path calls _make_container_spec once per method against
    # the same volume list. Every build must produce identical types — this is
    # the regression that caused the CR to re-roll endlessly.
    volumes = [
        VolumeMount(name="v1", mount_path="/a", type="versioned_chalkfs"),
        VolumeMount(name="v2", mount_path="/b", type="chalkfs"),
    ]
    first = _spec(volumes)["volumes"]
    second = _spec(volumes)["volumes"]
    assert first == second


def test_missing_type_raises_rather_than_guessing() -> None:
    volumes = [VolumeMount(name="bad", mount_path="/x", type="")]
    with pytest.raises(VolumeError, match="no mount type"):
        _spec(volumes)


def test_no_volumes_omits_volumes_key() -> None:
    assert "volumes" not in _spec([])


def test_versioned_client_stamps_versioned_type(tmp_path) -> None:
    from chalkcompute._image import Image, LazyLocalFile
    from chalkcompute._vvolume import VersionedVolumeClient

    src = tmp_path / "f.txt"
    src.write_bytes(b"hi")

    image = Image.base("python:3.14")
    image._lazy_local_files = {LazyLocalFile(src=str(src), dest="/app/f.txt")}

    client = object.__new__(VersionedVolumeClient)
    mock_vol = MagicMock()
    mock_vol.name = "vv-1"
    # batch_upload is a context manager returning a batch with put_file.
    batch = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=batch)
    cm.__exit__ = MagicMock(return_value=False)
    mock_vol.batch_upload = MagicMock(return_value=cm)
    client.create = MagicMock(return_value=mock_vol)

    result = client.upload_lazy_files(image)
    assert result == [
        VolumeMount(name="vv-1", mount_path="/app", type="versioned_chalkfs")
    ]


def test_simple_client_stamps_chalkfs_type(tmp_path) -> None:
    from chalkcompute._image import Image, LazyLocalFile
    from chalkcompute._volume import VolumeClient

    src = tmp_path / "f.txt"
    src.write_bytes(b"hi")

    image = Image.base("python:3.14")
    image._lazy_local_files = {LazyLocalFile(src=str(src), dest="/app/f.txt")}

    client = object.__new__(VolumeClient)
    client._metadata = []
    client._stub = MagicMock()
    mock_vol = MagicMock()
    mock_vol.name = "sv-1"
    client.create = MagicMock(return_value=mock_vol)

    with patch("chalkcompute._volume._upload_bytes"):
        result = client.upload_lazy_files(image)
    assert result == [VolumeMount(name="sv-1", mount_path="/app", type="chalkfs")]


# ---------------------------------------------------------------------------
# resolve_referenced_volume_type: a referenced volume's type comes from its
# actual backend (probed at deploy time), never a hardcoded default.
# ---------------------------------------------------------------------------


def _v1_client_with_get(side_effect=None, found: bool = True) -> VolumeClient:
    client = object.__new__(VolumeClient)
    client._metadata = []
    client._stub = MagicMock()
    if side_effect is not None:
        client._stub.GetVolume.side_effect = side_effect
    elif not found:
        client._stub.GetVolume.side_effect = _FakeRpcError(grpc.StatusCode.NOT_FOUND)
    return client


def _v2_client_with_get(side_effect=None, found: bool = True) -> VersionedVolumeClient:
    client = object.__new__(VersionedVolumeClient)
    if side_effect is not None:
        client.exists = MagicMock(side_effect=side_effect)
    elif not found:
        client.exists = MagicMock(return_value=False)
    else:
        client.exists = MagicMock(return_value=True)
    return client


def test_resolve_referenced_type_versioned_when_in_v2() -> None:
    v2 = _v2_client_with_get(found=True)
    v1 = _v1_client_with_get(found=True)
    connect = MagicMock()
    with (
        patch.object(VersionedVolumeClient, "from_connect", return_value=v2),
        patch.object(VolumeClient, "from_connect", return_value=v1),
    ):
        assert resolve_referenced_volume_type(connect, "vol") == "versioned_chalkfs"
    # v2 hit short-circuits; v1 is never probed.
    v2.exists.assert_called_once_with("vol")
    v1._stub.GetVolume.assert_not_called()


def test_resolve_referenced_type_chalkfs_when_only_in_v1() -> None:
    v2 = _v2_client_with_get(found=False)
    v1 = _v1_client_with_get(found=True)
    connect = MagicMock()
    with (
        patch.object(VersionedVolumeClient, "from_connect", return_value=v2),
        patch.object(VolumeClient, "from_connect", return_value=v1),
    ):
        assert resolve_referenced_volume_type(connect, "vol") == "chalkfs"
    v2.exists.assert_called_once_with("vol")
    v1._stub.GetVolume.assert_called_once()


def test_resolve_referenced_type_raises_when_in_neither() -> None:
    v2 = _v2_client_with_get(found=False)
    v1 = _v1_client_with_get(found=False)
    connect = MagicMock()
    with (
        patch.object(VersionedVolumeClient, "from_connect", return_value=v2),
        patch.object(VolumeClient, "from_connect", return_value=v1),
    ):
        with pytest.raises(VolumeError, match="missing-vol"):
            resolve_referenced_volume_type(connect, "missing-vol")
    v2.exists.assert_called_once_with("missing-vol")


def test_resolve_referenced_type_propagates_non_not_found() -> None:
    # A transient/auth failure must propagate, not be swallowed into a default.
    v2 = _v2_client_with_get(
        side_effect=_FakeRpcError(grpc.StatusCode.PERMISSION_DENIED)
    )
    v1 = _v1_client_with_get(found=True)
    connect = MagicMock()
    with (
        patch.object(VersionedVolumeClient, "from_connect", return_value=v2),
        patch.object(VolumeClient, "from_connect", return_value=v1),
    ):
        with pytest.raises(grpc.RpcError):
            resolve_referenced_volume_type(connect, "vol")
    v2.exists.assert_called_once_with("vol")
    v1._stub.GetVolume.assert_not_called()


def test_referenced_base_volume_in_deploy_uses_looked_up_type() -> None:
    # A referenced base volume in a ScalingGroup deploy ends up with the
    # backend-looked-up type rather than a hardcoded default.
    from chalkcompute._scaling_group import ScalingGroup

    sg = ScalingGroup(
        image="ubuntu:latest", name="sg-test", volumes=[("old-vol", "/data")]
    )
    # Stored as a raw reference, not yet typed.
    assert sg._referenced_volumes == [("old-vol", "/data")]

    fake_client = MagicMock()
    sg._client = fake_client

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"scalingGroup": {"id": "sg-1", "name": "sg-test"}}
    fake_client.rpc.return_value = resp

    info = MagicMock()
    info.id = "sg-1"
    info.status = "Running"

    with (
        patch(
            "chalkcompute._scaling_group.resolve_referenced_volume_type",
            return_value="chalkfs",
        ) as resolve,
        patch(
            "chalkcompute._scaling_group._parse_scaling_group_response",
            return_value=info,
        ),
    ):
        sg.deploy()

    resolve.assert_called_once_with(fake_client, "old-vol")
    assert sg._volumes == [
        VolumeMount(name="old-vol", mount_path="/data", type="chalkfs")
    ]
    # The looked-up type is what reaches the CreateScalingGroup spec.
    _svc, _method, body = fake_client.rpc.call_args[0][:3]
    assert body["spec"]["containerSpec"]["volumes"] == [
        {"name": "old-vol", "mountPath": "/data", "type": "chalkfs"}
    ]
