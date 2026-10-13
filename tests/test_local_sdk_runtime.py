import hashlib
import json
import os
import shlex
import subprocess
import sys
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from chalkcompute import Image
from chalkcompute import _local_dev as local
from chalkcompute import _local_sdk_runtime as runtime


@pytest.fixture
def wheel(tmp_path, monkeypatch):
    monkeypatch.setenv("CHALK_FUNCTION_STRIP_SOURCE", "0")
    monkeypatch.delenv("CHALK_FUNCTION_REMOTE_CALL_WHEEL", raising=False)
    monkeypatch.delenv("CHALK_FUNCTION_LOCAL_SDK_BAKED_RUNTIME", raising=False)
    path = tmp_path / "chalkcompute-0.0.0-cp314-cp314-linux_x86_64.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "chalkcompute-0.0.0.dist-info/WHEEL", "Tag: cp314-cp314-linux_x86_64\n"
        )
    monkeypatch.setenv("CHALK_FUNCTION_LOCAL_SDK_WHEEL", str(path))
    return path


def configure_baked(wheel: Path, monkeypatch, base: Image) -> local.BakedSDKRuntime:
    recipe = local.local_sdk_dependency_image(base, "chalk-remote-call-python==1.10.4")
    binding = local.BakedSDKRuntime(
        "k3d-chalk-registry.localhost:5000/chalk/local-sdk-runtime@sha256:" + "a" * 64,
        hashlib.sha256(wheel.read_bytes()).hexdigest(),
        local.image_recipe_sha256(recipe),
    )
    monkeypatch.setenv(
        "CHALK_FUNCTION_LOCAL_SDK_BAKED_RUNTIME", json.dumps(binding.__dict__)
    )
    return binding


def test_verified_runtime_removes_sdk_mount_but_preserves_user_files(
    wheel, monkeypatch, tmp_path
):
    source = tmp_path / "source.py"
    source.write_text("pass")
    base = Image.debian_slim().add_local_file(str(source), "/app/source.py")
    binding = configure_baked(wheel, monkeypatch, base)
    image = local.apply_local_dev_mounts(
        base, remote_call_runtime_package="chalk-remote-call-python==1.10.4"
    )
    assert image._base_image == binding.image
    assert image._steps == []
    assert image.lazy_local_files == base.lazy_local_files
    command = local.wrap_entrypoint_for_local_dev(["handler"], image)[2]
    assert "direct_url.json" in command
    assert binding.wheel_sha256 in command
    assert "pip install" not in command
    assert "/tmp/sdk" not in command


def test_custom_recipe_keeps_mount_and_install(wheel, monkeypatch):
    configure_baked(wheel, monkeypatch, Image.debian_slim())
    custom = Image.debian_slim().run_commands("echo custom")
    image = local.apply_local_dev_mounts(
        custom, remote_call_runtime_package="chalk-remote-call-python==1.10.4"
    )
    assert image._base_image == custom._base_image
    assert any(f.dest.startswith("/tmp/sdk/") for f in image.lazy_local_files)
    assert "pip install" in local.wrap_entrypoint_for_local_dev(["handler"], image)[2]


def test_changed_wheel_rejects_stale_runtime_before_deploy(wheel, monkeypatch):
    configure_baked(wheel, monkeypatch, Image.debian_slim())
    wheel.write_bytes(b"changed")
    with pytest.raises(ValueError, match="wheel changed"):
        local.apply_local_dev_mounts(
            Image.debian_slim(),
            remote_call_runtime_package="chalk-remote-call-python==1.10.4",
        )


@pytest.mark.parametrize("mode", ["published", "source-overlay", "strip-source"])
def test_nonwheel_paths_ignore_baked_configuration(wheel, monkeypatch, mode):
    monkeypatch.setenv(
        "CHALK_FUNCTION_LOCAL_SDK_BAKED_RUNTIME", "invalid stale binding"
    )
    if mode == "strip-source":
        monkeypatch.setenv("CHALK_FUNCTION_STRIP_SOURCE", "1")
    else:
        monkeypatch.delenv("CHALK_FUNCTION_LOCAL_SDK_WHEEL")
        monkeypatch.setenv(
            "CHALK_FUNCTION_LOCAL_SDK_COPY", "1" if mode == "source-overlay" else "0"
        )
    config = local.resolve_local_deploy_config()
    assert config.baked_runtime is None
    command = local.wrap_entrypoint_for_local_dev(["handler"], None)
    if mode == "source-overlay":
        assert "tar xzf" in command[2]
    else:
        assert command == ["handler"]


@pytest.mark.parametrize("installed_hash", ["matching", "wrong", "missing"])
def test_entrypoint_checks_installed_archive_identity(
    wheel, monkeypatch, tmp_path, installed_hash
):
    binding = configure_baked(wheel, monkeypatch, Image.debian_slim())
    image = local.apply_local_dev_mounts(
        Image.debian_slim(),
        remote_call_runtime_package="chalk-remote-call-python==1.10.4",
    )
    metadata = tmp_path / "chalkcompute-0.0.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: chalkcompute\nVersion: 0.0.0\n")
    info = (
        {}
        if installed_hash == "missing"
        else {
            "archive_info": {
                "hashes": {
                    "sha256": binding.wheel_sha256
                    if installed_hash == "matching"
                    else "f" * 64
                }
            }
        }
    )
    (metadata / "direct_url.json").write_text(json.dumps(info))
    command = local.wrap_entrypoint_for_local_dev(
        [sys.executable, "-c", "print('HANDLER_STARTED')"], image
    )
    command[2] = command[2].replace("python -c", shlex.quote(sys.executable) + " -c", 1)
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(tmp_path), "PYTHONOPTIMIZE": "1"},
    )
    assert (result.returncode == 0) == (installed_hash == "matching")
    assert ("HANDLER_STARTED" in result.stdout) == (installed_hash == "matching")


def test_unsupported_wheel_fails_before_base_resolution(wheel):
    unsupported = wheel.with_name(
        wheel.name.replace("linux_x86_64", "macosx_11_0_arm64")
    )
    unsupported.write_bytes(wheel.read_bytes())
    with patch.object(Image, "build") as build:
        with pytest.raises(ValueError, match="Linux x86_64"):
            runtime.bake_runtime(
                unsupported,
                Image.debian_slim(),
                "localhost:5000",
                "k3d-chalk-registry.localhost:5000",
            )
        build.assert_not_called()


def test_bake_cache_hit_never_runs_docker(wheel):
    base_digest = "sha256:" + "b" * 64
    wheel_sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
    labels = {
        "dev.chalk.local-sdk." + k: v
        for k, v in dict(
            wheel_sha256=wheel_sha,
            base_digest=base_digest,
            platform="linux/amd64",
            recipe_version="1",
        ).items()
    }
    cached = dict(os="linux", architecture="amd64", config={"Labels": labels})
    with (
        patch.object(
            Image,
            "build",
            return_value=Image.base("k3d-chalk-registry.localhost:5000/base:tag"),
        ),
        patch.object(
            runtime,
            "_registry_image",
            side_effect=[
                (base_digest, dict(os="linux", architecture="amd64")),
                ("sha256:" + "c" * 64, cached),
            ],
        ),
        patch.object(runtime.subprocess, "run") as run,
    ):
        binding = runtime.bake_runtime(
            wheel,
            Image.debian_slim(),
            "localhost:5000",
            "k3d-chalk-registry.localhost:5000",
        )
        assert binding.image.endswith("@sha256:" + "c" * 64)
        run.assert_not_called()


def test_cache_metadata_mismatch_is_not_reused(wheel):
    with (
        patch.object(
            Image,
            "build",
            return_value=Image.base("k3d-chalk-registry.localhost:5000/base:tag"),
        ),
        patch.object(
            runtime,
            "_registry_image",
            side_effect=[
                ("sha256:" + "b" * 64, dict(os="linux", architecture="amd64")),
                (
                    "sha256:" + "c" * 64,
                    dict(os="linux", architecture="amd64", config={"Labels": {}}),
                ),
            ],
        ),
        patch.object(runtime.subprocess, "run") as run,
    ):
        with pytest.raises(ValueError, match="Cached local SDK runtime metadata"):
            runtime.bake_runtime(
                wheel,
                Image.debian_slim(),
                "localhost:5000",
                "k3d-chalk-registry.localhost:5000",
            )
        run.assert_not_called()


def test_default_function_recipe_matches_baked_helper(wheel, monkeypatch):
    from chalkcompute._function import RemoteFunction, _make_base_image

    def sync_add(left: int, right: int) -> int:
        return left + right

    function = RemoteFunction(sync_add)
    package = function._remote_call_runtime_package()
    assert "[tracing]" in package
    recipe = runtime.standard_dependency_image("3.14")
    binding = local.BakedSDKRuntime(
        "k3d-chalk-registry.localhost:5000/chalk/local-sdk-runtime@sha256:" + "a" * 64,
        hashlib.sha256(wheel.read_bytes()).hexdigest(),
        local.image_recipe_sha256(recipe),
    )
    monkeypatch.setenv(
        "CHALK_FUNCTION_LOCAL_SDK_BAKED_RUNTIME", json.dumps(binding.__dict__)
    )
    image = _make_base_image(None, remote_call_runtime_package=package)
    assert image._base_image == binding.image
    assert not image.lazy_local_files


def test_manylinux_compressed_tags_accept_expanded_metadata(wheel):
    name = wheel.with_name(
        "chalkcompute-0.0.0-cp314-cp314-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
    )
    with zipfile.ZipFile(name, "w") as archive:
        archive.writestr(
            "chalkcompute-0.0.0.dist-info/WHEEL",
            "Tag: cp314-cp314-manylinux_2_17_x86_64\nTag: cp314-cp314-manylinux2014_x86_64\n",
        )
    assert runtime.wheel_platform(name) == "linux/amd64"


def test_base_architecture_mismatch_fails_without_docker(wheel):
    with (
        patch.object(
            Image,
            "build",
            return_value=Image.base("k3d-chalk-registry.localhost:5000/base:tag"),
        ),
        patch.object(
            runtime,
            "_registry_image",
            return_value=("sha256:" + "b" * 64, dict(os="linux", architecture="arm64")),
        ),
        patch.object(runtime.subprocess, "run") as run,
    ):
        with pytest.raises(ValueError, match="wheel architecture"):
            runtime.bake_runtime(
                wheel,
                Image.debian_slim(),
                "localhost:5000",
                "k3d-chalk-registry.localhost:5000",
            )
        run.assert_not_called()


def test_cache_miss_builds_only_exact_wheel_on_pinned_base(wheel):
    base_digest = "sha256:" + "b" * 64
    wheel_sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
    labels = {
        "dev.chalk.local-sdk." + k: v
        for k, v in dict(
            wheel_sha256=wheel_sha,
            base_digest=base_digest,
            platform="linux/amd64",
            recipe_version="1",
        ).items()
    }
    cached = dict(os="linux", architecture="amd64", config={"Labels": labels})

    def verify_build(command, check):
        context = Path(command[-1])
        assert {p.name for p in context.iterdir()} == {wheel.name, "Dockerfile"}
        assert (context / wheel.name).read_bytes() == wheel.read_bytes()
        recipe = (context / "Dockerfile").read_text()
        assert f"FROM localhost:5000/base@{base_digest}" in recipe
        assert "direct_url.json" in recipe and wheel_sha in recipe
        assert "--push" in command and "--provenance=false" in command
        return subprocess.CompletedProcess(command, 0)

    missing = urllib.error.HTTPError("local", 404, "missing", {}, None)
    with (
        patch.object(
            Image,
            "build",
            return_value=Image.base("k3d-chalk-registry.localhost:5000/base:tag"),
        ),
        patch.object(
            runtime,
            "_registry_image",
            side_effect=[
                (base_digest, dict(os="linux", architecture="amd64")),
                missing,
                ("sha256:" + "c" * 64, cached),
            ],
        ),
        patch.object(runtime.subprocess, "run", side_effect=verify_build) as run,
    ):
        runtime.bake_runtime(
            wheel,
            Image.debian_slim(),
            "localhost:5000",
            "k3d-chalk-registry.localhost:5000",
        )
        run.assert_called_once()
