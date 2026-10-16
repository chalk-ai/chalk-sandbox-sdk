from functools import partial
import json
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from chalkcompute import Image, VolumeMount
from chalkcompute import _connect
from chalkcompute import _image_build
from chalkcompute._cls import RemoteClass, method
from chalkcompute._container import Container
from chalkcompute._function import RemoteFunction
from chalkcompute._gen.chalk.container.v1 import service_pb as container_pb
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from chalkcompute._sandbox import Sandbox
from chalkcompute._scaling_group import ScalingGroup


@pytest.mark.parametrize("inspection_fails", [False, True])
def test_scaling_group_delete_cleans_up_lazy_volumes(inspection_fails):
    current = VolumeMount("chalkcompute-sg-files-current", "/app", 7)
    historical = "chalkcompute-sg-files-old"
    revisions = sg_pb.ListScalingGroupRevisionsResponse(
        revisions=[
            sg_pb.ScalingGroupRevisionResponse(
                spec=sg_pb.ScalingGroupSpec(
                    container_spec=container_pb.ChalkContainerSpec(
                        volumes=[
                            container_pb.VolumeMount(
                                name=historical, mount_path="/app", version_id=7
                            ),
                            container_pb.VolumeMount(
                                name="user-data", mount_path="/data"
                            ),
                        ]
                    )
                )
            )
        ]
    )
    sg = ScalingGroup(image="nginx:alpine")
    sg._scaling_group_id = "sg-1"
    sg._owned_volumes = [current]
    driver = MagicMock()
    sg._volume_driver = driver
    sg._client = MagicMock()
    sg._client.call.side_effect = [
        ValueError("invalid revision") if inspection_fails else revisions,
        sg_pb.DeleteScalingGroupResponse(),
    ]

    sg.delete()

    assert (
        sg._client.call.call_args.args[0]
        == sg._client.stub.return_value.delete_scaling_group
    )
    assert sg._client.call.call_args.args[1].id == "sg-1"
    deleted = [args.args[0] for args in driver.delete_volume.call_args_list]
    assert deleted == (
        [current.name] if inspection_fails else [current.name, historical]
    )
    assert sg._scaling_group_id is None
    assert sg._owned_volumes == []


def built_image(uri, volumes=()):
    return Image.from_json(
        json.dumps(
            {
                "schema_version": 1,
                "image_uri": uri,
                "volumes": [
                    {
                        "name": m.name,
                        "mount_path": m.mount_path,
                        "version_id": m.version_id,
                    }
                    for m in volumes
                ],
            }
        )
    )


def identity(value: int) -> int:
    return value


class Worker:
    @method
    def identity(self, value: int) -> int:
        return value


def test_round_trip_without_services():
    spec = built_image("engine:1", (VolumeMount("assets", "/assets", 7),))
    assert Image.from_json(spec.to_json()).to_json() == spec.to_json()
    assert Sandbox(image=Image.from_json(spec.to_json()))._referenced_volumes == list(
        spec.volumes
    )


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 2},
        {"image_uri": ""},
        {"volumes": {}},
        {"volumes": [{"name": "assets", "mount_path": "/assets", "version_id": None}]},
        {"volumes": [{"name": "assets", "mount_path": "/assets", "version_id": -1}]},
        {"volumes": [{"name": "assets", "mount_path": "assets", "version_id": 7}]},
    ],
)
def test_invalid_serialized_spec(change):
    data = json.loads(built_image("engine:1").to_json())
    data.update(change)
    with pytest.raises(ValueError):
        Image.from_json(json.dumps(data))


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
def test_image_build_across_surfaces(factory):
    asset = VolumeMount("assets", "/assets", 7)
    spec = built_image("engine:1", (asset,))
    resource = factory(image=spec, volumes=[asset, ("saves", "/saves")])
    assert resource._image.as_image_uri_if_possible() == "engine:1"
    assert resource._referenced_volumes == [asset, VolumeMount("saves", "/saves")]
    with pytest.raises(ValueError, match="Conflicting"):
        factory(image=spec, volumes=[("other", "/assets")])

    nested = VolumeMount("nested", "/assets/subdir", 8)
    plain = factory(image=Image.base("engine:1"), volumes=[asset, nested])
    pinned = factory(image=spec, volumes=[nested])
    restored = factory(image=built_image("engine:1", (asset, nested)))
    assert (
        plain._referenced_volumes
        == pinned._referenced_volumes
        == restored._referenced_volumes
    )


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
    with pytest.raises(ValueError, match=r"Image.build"):
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
    monkeypatch.setattr(_image_build.VolumeClient, "from_env", from_env)
    build_image = MagicMock(return_value="engine:1")
    monkeypatch.setattr(_image_build, "build_image", build_image)
    spec = image.build(
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


def test_resolve_lazy_files_defaults_name(tmp_path, monkeypatch):
    source = tmp_path / "bridge.py"
    source.write_text("pass")
    image = Image.base("engine:1").add_local_file(str(source), "/assets/bridge.py")
    name = _image_build.default_source_volume_name(image)
    driver = MagicMock()
    driver._resolve_lazy_files.return_value = [VolumeMount(name, "/assets", 11)]
    context = MagicMock()
    context.__enter__.return_value = driver
    from_env = MagicMock(return_value=context)
    monkeypatch.setattr(_image_build.VolumeClient, "from_env", from_env)
    monkeypatch.setattr(_image_build, "build_image", MagicMock(return_value="engine:1"))

    spec = image.build()

    assert spec.volumes == (VolumeMount(name, "/assets", 11),)
    driver._resolve_lazy_files.assert_called_once_with(
        image, volume_name=name, poll_interval=0.5, timeout=600.0
    )


def test_default_source_volume_name_is_stable(tmp_path):
    source = tmp_path / "bridge.py"
    other = tmp_path / "other.py"
    source.write_text("old bridge")
    other.write_text("different bridge")
    recipe = Image.base("engine:1").env({"A": "1", "B": "2"})
    image = recipe.add_local_file(str(source), "/app/bridge.py")
    default_name = _image_build.default_source_volume_name
    name = default_name(image)

    assert re.fullmatch(r"chalkcompute-image-[0-9a-f]{12}", name)
    source.write_text("new bridge")
    assert default_name(image) == name
    assert default_name(recipe.add_local_file(str(other), "/app/bridge.py")) == name
    assert (
        default_name(
            Image.base("engine:1")
            .env({"B": "2", "A": "1"})
            .add_local_file(str(other), "/app/bridge.py")
        )
        == name
    )
    assert default_name(recipe.add_local_file(str(source), "/app/other.py")) != name
    for mode in (0, 0o644, 0o755):
        assert (
            default_name(
                recipe.add_local_file(str(source), "/app/bridge.py", mode=mode)
            )
            != name
        )
    assert default_name(image.run_commands("true")) != name

    first = image.add_local_file(str(other), "/app/other.py")
    second = recipe.add_local_file(str(other), "/app/other.py").add_local_file(
        str(source), "/app/bridge.py"
    )
    assert default_name(first) == default_name(second)
    assert default_name(first) != name


def test_resolve_invalid_mount_before_side_effects(tmp_path, monkeypatch):
    source = tmp_path / "bridge.py"
    source.write_text("pass")
    image = Image.base("engine:1").add_local_file(str(source), "/assets/bridge.py")
    from_env = MagicMock()
    monkeypatch.setattr(_image_build.VolumeClient, "from_env", from_env)
    with pytest.raises(ValueError, match="Conflicting"):
        image.build(
            name="bridge",
            volumes=[VolumeMount("other", "/assets")],
        )
    from_env.assert_not_called()


@pytest.mark.parametrize(
    "factory",
    [
        Sandbox,
        Container,
        ScalingGroup,
        partial(RemoteFunction, identity),
        partial(RemoteClass, Worker),
        lambda image: image.build(),
        lambda image: _connect.build_image_with_volumes(MagicMock(), image),
    ],
)
@pytest.mark.parametrize("mount_path", ["/assets", "/assets/./"])
def test_chained_files_reject_pinned_mount_conflicts(
    tmp_path, monkeypatch, factory, mount_path
):
    source = tmp_path / "new.txt"
    source.write_text("new content")
    original = built_image("engine:1", (VolumeMount("assets", mount_path, 7),))
    image = original.add_local_file(str(source), "/assets/new.txt")
    client = MagicMock(side_effect=AssertionError("unexpected volume client"))
    monkeypatch.setattr(_image_build.VolumeClient, "from_env", client)
    monkeypatch.setattr(_image_build.VolumeClient, "from_connect", client)

    with pytest.raises(
        ValueError, match="Conflicting volume mount destination: /assets"
    ):
        factory(image=image)
    client.assert_not_called()
    assert original.volumes == image.volumes


def test_local_files_cannot_share_mount_with_volume_named_source(tmp_path, monkeypatch):
    source = tmp_path / "new.txt"
    source.write_text("new content")
    image = Image.base("engine:1").add_local_file(str(source), "/assets/new.txt")
    client = MagicMock(side_effect=AssertionError("unexpected volume client"))
    monkeypatch.setattr(_image_build.VolumeClient, "from_env", client)

    with pytest.raises(
        ValueError, match="Conflicting volume mount destination: /assets"
    ):
        image.build(volumes=[("source", "/assets")])
    client.assert_not_called()


def test_resolved_assets_are_not_worker_owned():
    from chalkcompute._volume import cleanup_owned_volumes

    spec = built_image("engine:1", (VolumeMount("assets", "/assets", 7),))
    sandbox = Sandbox(image=spec)
    client = MagicMock()
    cleanup_owned_volumes(client, spec.volumes, sandbox._referenced_volumes)
    client.delete_volume.assert_not_called()


def test_scaling_group_cleanup_preserves_referenced_resolved_assets():
    asset = VolumeMount("scaling-group-files-shared", "/assets", 7)
    group = ScalingGroup(image=built_image("engine:1", (asset,)))
    driver = MagicMock()
    group._volume_driver = driver
    group._owned_volumes = [asset]
    group.delete()
    driver.delete_volume.assert_not_called()


def test_build_returns_new_image_and_chaining_requires_rebuild(monkeypatch):
    recipe = Image.base("engine:1")
    build = MagicMock(return_value=built_image("engine:built"))
    monkeypatch.setattr(_image_build, "build_image_snapshot", build)
    with pytest.raises(ValueError, match="build"):
        recipe.to_json()
    built = recipe.build()
    assert isinstance(built, Image)
    assert recipe.as_image_uri_if_possible() == "engine:1"
    assert built.as_image_uri_if_possible() == "engine:built"
    assert built.build().to_json() == built.to_json()
    build.assert_called_once()
    with pytest.raises(ValueError, match="build"):
        built.env({"MODE": "test"}).to_json()


@pytest.mark.parametrize("volumes", [None, [], iter(())])
def test_repeated_build_needs_no_services(monkeypatch, volumes):
    image = built_image("engine:1", (VolumeMount("assets", "/assets", 0),))
    build = MagicMock(side_effect=AssertionError("unexpected build"))
    monkeypatch.setattr(_image_build, "build_image_snapshot", build)

    repeated = image.build(
        volumes=volumes, chalk_client=object(), poll_interval=2, timeout=15
    )

    assert repeated is not image
    assert repeated.to_json() == image.to_json()
    build.assert_not_called()


@pytest.mark.parametrize("built", [False, True])
def test_source_name_requires_local_volume_files(monkeypatch, built):
    image = built_image("engine:1") if built else Image.base("engine:1")
    build = MagicMock(side_effect=AssertionError("unexpected build"))
    monkeypatch.setattr(_image_build, "build_image_snapshot", build)

    with pytest.raises(ValueError, match="requires local volume files"):
        image.build(name="renamed")
    build.assert_not_called()


def test_built_image_repr_distinguishes_pins_and_build_state():
    image = built_image("engine:1", (VolumeMount("assets", "/assets", 0),))
    assert repr(image) == "Image(base='engine:1', built=True, volumes=1)"
    assert repr(Image.base("engine:1")) == "Image(base='engine:1')"
    assert "volumes=1" in repr(image.run_commands("true"))
    assert "built=True" not in repr(image.run_commands("true"))


def test_chaining_preserves_pins_and_legacy_helpers_do_not_drop_them(monkeypatch):
    image = built_image("engine:1", (VolumeMount("assets", "/assets", 7),))
    extended = image.run_commands("true")
    assert extended.volumes == image.volumes
    assert Sandbox(image=extended)._referenced_volumes == list(image.volumes)
    with pytest.raises(ValueError, match="cannot preserve"):
        extended.to_proto()
    with pytest.raises(ValueError, match="Image.build"):
        _connect.build_image_from_spec(image)
    for pinned in (image, extended):
        with pytest.raises(ValueError, match="cannot preserve"):
            _connect.build_image(MagicMock(), pinned)
    assert _connect.build_image_with_volumes(MagicMock(), image) == (
        "engine:1",
        list(image.volumes),
    )
    recipe = Image.base("engine:1").env({"MODE": "test"})
    assert (
        Image.from_proto_bytes(recipe.to_proto().SerializeToString()).to_proto()
        == recipe.to_proto()
    )


@pytest.mark.parametrize("helper", ["snapshot", "legacy"])
def test_build_extended_image_preserves_existing_and_nested_content(
    tmp_path, monkeypatch, helper
):
    parent = VolumeMount("assets", "/assets", 7)
    child = VolumeMount("new-files", "/assets/subdir", 9)
    source = tmp_path / "extra.txt"
    source.write_text("extra")
    original = built_image("engine:1", (parent,))
    extended = original.run_commands("true").add_local_file(
        str(source), "/assets/subdir/extra.txt"
    )
    driver = MagicMock()
    driver._connect.call.return_value = SimpleNamespace(
        status="exists", image="engine:2"
    )
    driver._get_volume.return_value.version.version_id = 7
    driver._resolve_lazy_files.return_value = [child]
    driver.upload_lazy_files.return_value = [child]
    driver.__enter__.return_value = driver
    monkeypatch.setattr(_image_build.VolumeClient, "from_env", lambda **kwargs: driver)
    monkeypatch.setattr(
        _image_build.VolumeClient, "from_connect", lambda client: driver
    )

    if helper == "snapshot":
        result = extended.build(name="new-files")
        assert isinstance(result, Image)
        assert result.as_image_uri_if_possible() == "engine:2"
        mounts = list(result.volumes)
        assert Image.from_json(result.to_json()).volumes == result.volumes
    else:
        uri, mounts = _connect.build_image_with_volumes(driver._connect, extended)
        assert uri == "engine:2"

    assert mounts == [parent, child]
    request = driver._connect.call.call_args.args[1]
    assert request.image_spec.base_image == "engine:1"
    assert len(request.image_spec.steps) == 1
    assert original.volumes == extended.volumes == (parent,)
    with pytest.raises(ValueError, match="cannot preserve"):
        extended.to_proto()


@pytest.mark.parametrize(
    "factory", [partial(RemoteFunction, identity), partial(RemoteClass, Worker)]
)
@pytest.mark.parametrize("mount_path", ["/app", "/assets", "/app/data", "/app/tests"])
def test_deploy_with_pinned_content(factory, monkeypatch, mount_path):
    from pathlib import Path
    from chalkcompute import _function_deployment as deployment
    from tests.test_function_deployment import Client, complete

    monkeypatch.setenv("CHALK_FUNCTION_STRIP_SOURCE", "0")
    image = built_image("engine:1", (VolumeMount("content", mount_path, 7),))
    resource = factory(image=image, env={"PYTHONPATH": "/custom"})
    client = Client([complete()])
    client._env_id = None
    resource._client = client
    uploaded = {}

    def upload(image, **kwargs):
        uploaded.update(
            {file.dest: Path(file.src).read_text() for file in image.lazy_local_files}
        )
        return [
            VolumeMount("generated", path, 9)
            for path in image.lazy_local_files_grouped_for_volume_mounts()
        ]

    driver = MagicMock()
    driver._resolve_lazy_files.side_effect = upload
    monkeypatch.setattr(deployment.VolumeClient, "from_connect", lambda client: driver)
    resource.deploy()
    container = client.requests[0].spec.version_spec.spec.container_spec
    mounts = {m.mount_path: (m.name, m.version_id) for m in container.volumes}
    relocated = mount_path == "/app" or (
        mount_path == "/app/tests" and isinstance(resource, RemoteFunction)
    )
    root = "/__chalk_source" if relocated else "/app"
    assert mounts[mount_path] == ("content", 7)
    assert mounts[root] == ("generated", 9)
    assert container.env_vars["PYTHONPATH"] == (
        "/__chalk_source:/app:/custom" if relocated else "/custom"
    )
    assert uploaded and all(path.startswith(root + "/") for path in uploaded)
    for path, source in uploaded.items():
        compile(source, path, "exec")
