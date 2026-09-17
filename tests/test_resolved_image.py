from functools import partial
import json
from unittest.mock import MagicMock

import pytest

from chalkcompute import Image, ResolvedImage, VolumeMount
from chalkcompute import _connect
from chalkcompute import _resolved_image
from chalkcompute._cls import RemoteClass, method
from chalkcompute._container import Container
from chalkcompute._function import RemoteFunction
from chalkcompute._sandbox import Sandbox
from chalkcompute._scaling_group import ScalingGroup


def identity(value: int) -> int:
    return value


class Worker:
    @method
    def identity(self, value: int) -> int:
        return value


def test_round_trip_without_services():
    spec = ResolvedImage("engine:1", (VolumeMount("assets", "/assets", 7),))
    assert ResolvedImage.from_json(spec.to_json()) == spec
    assert Sandbox(
        image=ResolvedImage.from_json(spec.to_json())
    )._referenced_volumes == list(spec.volumes)


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 2},
        {"image_uri": ""},
        {"volumes": [{"name": "assets", "mount_path": "/assets", "version_id": None}]},
        {"volumes": [{"name": "assets", "mount_path": "assets", "version_id": 7}]},
    ],
)
def test_invalid_serialized_spec(change):
    data = json.loads(ResolvedImage("engine:1").to_json())
    data.update(change)
    with pytest.raises(ValueError):
        ResolvedImage.from_json(json.dumps(data))


@pytest.mark.parametrize(
    "factory",
    [
        Sandbox,
        Container,
        ScalingGroup,
        partial(RemoteFunction, identity),
        partial(RemoteClass, Worker),
    ],
)
def test_resolved_image_across_surfaces(factory):
    asset = VolumeMount("assets", "/assets", 7)
    spec = ResolvedImage("engine:1", (asset,))
    resource = factory(image=spec, volumes=[asset, ("saves", "/saves")])
    assert resource._image.as_image_uri_if_possible() == "engine:1"
    assert resource._referenced_volumes == [asset, VolumeMount("saves", "/saves")]
    with pytest.raises(ValueError, match="Conflicting"):
        factory(image=spec, volumes=[("other", "/assets/subdir")])


@pytest.mark.parametrize("commands", [False, True])
def test_uri_builder_rejects_lazy_files_before_client_creation(
    tmp_path, monkeypatch, commands
):
    source = tmp_path / "bridge.py"
    source.write_text("pass")
    image = Image.base("engine:1").add_local_file(str(source), "/assets/bridge.py")
    if commands:
        image = image.run_commands("true")
    client = MagicMock()
    monkeypatch.setattr(_connect, "ConnectClient", client)
    with pytest.raises(ValueError, match="resolve_image"):
        _connect.build_image_from_spec(image)
    client.assert_not_called()


def test_resolve_pins_ref_and_uploads(tmp_path, monkeypatch):
    source = tmp_path / "bridge.py"
    source.write_text("pass")
    image = Image.base("engine:1").add_local_file(str(source), "/assets/bridge.py")
    driver = MagicMock()
    driver._get_volume.return_value.version.version_id = 9
    driver._resolve_lazy_files.return_value = [
        VolumeMount("bridge-assets", "/assets", 11)
    ]
    context = MagicMock()
    context.__enter__.return_value = driver
    from_env = MagicMock(return_value=context)
    monkeypatch.setattr(_resolved_image.VolumeClient, "from_env", from_env)
    build_image = MagicMock(return_value="engine:1")
    monkeypatch.setattr(_resolved_image, "build_image", build_image)
    spec = _resolved_image.resolve_image(
        image,
        name="bridge",
        volumes=[VolumeMount("models", "/models", ref_name="released")],
    )
    assert spec.volumes == (
        VolumeMount("models", "/models", 9),
        VolumeMount("bridge-assets", "/assets", 11),
    )
    from_env.assert_called_once_with(chalk_client=None)
    build_image.assert_called_once_with(
        driver._connect,
        image,
        poll_interval=0.5,
        timeout=600.0,
    )
    driver._get_volume.assert_called_once_with(
        "models", version_id=None, ref_name="released"
    )
    driver._resolve_lazy_files.assert_called_once_with(
        image,
        volume_name="bridge",
        poll_interval=0.5,
        timeout=600.0,
    )


def test_resolve_lazy_files_requires_name_before_client_creation(tmp_path, monkeypatch):
    source = tmp_path / "bridge.py"
    source.write_text("pass")
    image = Image.base("engine:1").add_local_file(str(source), "/assets/bridge.py")
    from_env = MagicMock()
    monkeypatch.setattr(_resolved_image.VolumeClient, "from_env", from_env)

    with pytest.raises(ValueError, match=r"resolve_image\(name="):
        _resolved_image.resolve_image(image)

    from_env.assert_not_called()


def test_resolve_invalid_mount_before_side_effects(tmp_path, monkeypatch):
    source = tmp_path / "bridge.py"
    source.write_text("pass")
    image = Image.base("engine:1").add_local_file(str(source), "/assets/bridge.py")
    from_env = MagicMock()
    monkeypatch.setattr(_resolved_image.VolumeClient, "from_env", from_env)
    with pytest.raises(ValueError, match="Conflicting"):
        _resolved_image.resolve_image(
            image,
            name="bridge",
            volumes=[VolumeMount("other", "/assets")],
        )
    from_env.assert_not_called()


def test_resolved_assets_are_not_worker_owned():
    from chalkcompute._volume import cleanup_owned_volumes

    spec = ResolvedImage("engine:1", (VolumeMount("assets", "/assets", 7),))
    sandbox = Sandbox(image=spec)
    client = MagicMock()
    cleanup_owned_volumes(client, spec.volumes, sandbox._referenced_volumes)
    client.delete_volume.assert_not_called()


def test_scaling_group_cleanup_preserves_referenced_resolved_assets():
    asset = VolumeMount("scaling-group-files-shared", "/assets", 7)
    group = ScalingGroup(image=ResolvedImage("engine:1", (asset,)))
    driver = MagicMock()
    group._volume_driver = driver
    group._owned_volumes = [asset]
    group.delete()
    driver.delete_volume.assert_not_called()
