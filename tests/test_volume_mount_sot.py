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

import pytest

from chalkcompute._function import _make_container_spec
from chalkcompute._volume import VolumeError, VolumeMount


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
