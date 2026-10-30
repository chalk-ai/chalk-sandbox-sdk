from __future__ import annotations

import json
from functools import partial
from unittest.mock import MagicMock

import pytest

from chalkcompute import VersionedVolume
from chalkcompute import _volume as vv
from chalkcompute._cls import RemoteClass, method
from chalkcompute._container import Container
from chalkcompute._function import RemoteFunction
from chalkcompute._sandbox import Sandbox
from chalkcompute._scaling_group import ScalingGroup
from chalkcompute._volume import VolumeMount, VolumeMountType, normalize_volume_refs


def _identity(value: int) -> int:
    return value


class _Worker:
    @method
    def identity(self, value: int) -> int:
        return value


@pytest.mark.parametrize(
    "factory",
    [
        partial(Sandbox, image="ubuntu:latest"),
        partial(Container, image="ubuntu:latest"),
        partial(ScalingGroup, image="ubuntu:latest"),
        partial(RemoteFunction, _identity),
        partial(RemoteClass, _Worker),
    ],
    ids=["sandbox", "container", "scaling_group", "function", "class"],
)
@pytest.mark.parametrize(
    "version_id, ref, expected_ref",
    [(None, "main", None), (None, "sandbox-a", "sandbox-a"), (7, "main", None)],
    ids=["main", "fork", "pinned"],
)
def test_volume_handle_tuple_preserves_selector_across_surfaces(
    factory, version_id: int | None, ref: str, expected_ref: str | None
) -> None:
    volume_client = MagicMock()
    volume = VersionedVolume(
        "code", client=volume_client, version_id=version_id, ref=ref
    )
    volume_client.reset_mock()

    resource = factory(volumes=[(volume, "/code")])

    assert resource._referenced_volumes == [
        VolumeMount("code", "/code", version_id=version_id, ref_name=expected_ref)
    ]
    assert volume.version_id == version_id
    assert volume.ref == ref
    assert volume_client.mock_calls == []


@pytest.mark.parametrize("factory", [Sandbox, Container])
def test_mount_volume_accepts_handle_and_chains(factory) -> None:
    volume = VersionedVolume("code", client=MagicMock(), ref="sandbox-a")
    resource = factory(image="ubuntu:latest")

    assert resource.mount_volume(volume, "/code") is resource
    assert resource._referenced_volumes == [
        VolumeMount("code", "/code", ref_name="sandbox-a")
    ]


def test_normalize_mixed_volume_references() -> None:
    volume = VersionedVolume("code", client=MagicMock(), version_id=7)
    mount = VolumeMount("models", "/models")

    assert normalize_volume_refs(
        [("data", "/data"), volume, mount, (volume, "/code")]
    ) == [
        VolumeMount("data", "/data"),
        VolumeMount("code", "/volumes/code", version_id=7),
        mount,
        VolumeMount("code", "/code", version_id=7),
    ]


@pytest.mark.parametrize(
    "entry",
    [("code",), ("code", "/code", "extra"), (42, "/code"), ("code", 42)],
)
def test_normalize_volume_refs_rejects_malformed_entries(entry) -> None:
    with pytest.raises(ValueError, match="Each volumes entry"):
        normalize_volume_refs([entry])


def test_normalize_volume_handle_requires_string_mount_path() -> None:
    volume = VersionedVolume("code", client=MagicMock())
    with pytest.raises(ValueError, match="string mount path"):
        normalize_volume_refs([(volume, None)])  # type: ignore[arg-type]


@pytest.mark.parametrize("size_limit", [None, "64Mi"])
def test_empty_dir_mount_serialization_and_lifecycle(size_limit) -> None:
    from chalkcompute._containerspec import ContainerSpec
    from chalkcompute._gen.chalk.container.v1 import service_pb as container_pb

    mount = VolumeMount(
        "scratch", "/tmp", type=VolumeMountType.EMPTY_DIR, size_limit=size_limit
    )
    client = MagicMock()
    assert vv.normalize_volume_refs([mount]) == [mount]
    assert vv.referenced_volume_mount(client, mount) is mount
    assert vv.pinned_volume_mount(client, mount) is mount
    vv.cleanup_owned_volumes(client, [mount], [])
    assert not client.mock_calls

    spec = ContainerSpec(image="python:3.13", volumes=[mount])
    proto = spec.to_proto()
    assert proto.volumes[0].type == "empty_dir"
    assert proto.volumes[0].has_field("size_limit") == (size_limit is not None)
    if size_limit is not None:
        assert proto.volumes[0].size_limit == size_limit
    assert (
        container_pb.ChalkContainerSpec.from_json(json.dumps(spec.to_dict())) == proto
    )
