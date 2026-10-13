import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from chalkcompute._function import (
    RemoteFunction,
    _LOCAL_COPY_ENV_VAR,
    _LOCAL_REMOTE_CALL_WHEEL_ENV_VAR,
    _LOCAL_WHEEL_ENV_VAR,
    _make_base_image,
    _local_install_entrypoint,
)
from chalkcompute._local_dev import (
    build_function_runtime_image,
    function_runtime_image_or_none,
)


def test_local_sdk_copy_extracts_archive_to_local_tmp() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ[_LOCAL_COPY_ENV_VAR] = "1"
        os.environ.pop(_LOCAL_WHEEL_ENV_VAR, None)
        os.environ["CHALK_FUNCTION_STRIP_SOURCE"] = "0"
        entrypoint = _local_install_entrypoint(
            ["chalk-remote-call", "--handler", "handler.handler"], None
        )

    assert entrypoint[:2] == ["sh", "-c"]
    command = entrypoint[2]
    assert "cat /tmp/sdk/_archive.part.* > /tmp/_archive.tar.gz" in command
    assert (
        "tar xzf /tmp/_archive.tar.gz -C /tmp/chalkcompute-sdk-src/chalkcompute"
        in command
    )
    assert "export PYTHONPATH=/tmp/chalkcompute-sdk-src:$PYTHONPATH" in command
    assert "python -m pip install /tmp/chalkcompute-sdk-src" not in command
    assert "tar xzf /tmp/sdk/_archive.tar.gz -C /tmp/sdk" not in command


@pytest.mark.parametrize("layout", ["checkout", "site-packages"])
def test_sdk_overlay_preserves_installed_runtime(tmp_path, monkeypatch, layout):
    from chalkcompute import Image, _local_dev

    caller = tmp_path / layout
    sdk = caller / "chalkcompute"
    sdk.mkdir(parents=True)
    (sdk / "__init__.py").write_text("origin = 'local-sdk'\n")
    (sdk / "_local_dev.py").write_text("")
    # A sibling runtime must never travel with the SDK overlay. In practice
    # this can contain a native extension built for the caller's OS.
    sibling = caller / "chalk_remote_call"
    sibling.mkdir()
    (sibling / "__init__.py").write_text(
        "raise RuntimeError('caller runtime copied')\n"
    )
    installed = tmp_path / "container-site-packages" / "chalk_remote_call"
    installed.mkdir(parents=True)
    (installed / "__init__.py").write_text("origin = 'installed-runtime'\n")
    monkeypatch.setattr(_local_dev, "__file__", str(sdk / "_local_dev.py"))
    monkeypatch.setenv("CHALK_FUNCTION_LOCAL_SDK_COPY", "1")
    monkeypatch.setenv("CHALK_FUNCTION_STRIP_SOURCE", "0")
    monkeypatch.delenv("CHALK_FUNCTION_LOCAL_SDK_WHEEL", raising=False)
    monkeypatch.delenv("CHALK_FUNCTION_REMOTE_CALL_WHEEL", raising=False)
    image = _local_dev.apply_local_dev_mounts(
        Image.debian_slim(),
        remote_call_runtime_package="chalk-remote-call-python==1.9.2",
    )
    mount = tmp_path / "sdk-mount"
    mount.mkdir()
    for file in image.lazy_local_files:
        (mount / Path(file.dest).name).write_bytes(Path(file.src).read_bytes())
    command = _local_dev.wrap_entrypoint_for_local_dev(
        [
            sys.executable,
            "-c",
            "import chalkcompute, chalk_remote_call; "
            "assert chalkcompute.origin == 'local-sdk'; "
            "assert chalk_remote_call.origin == 'installed-runtime'",
        ],
        None,
    )
    command[2] = (
        command[2]
        .replace("/tmp/sdk", str(mount))
        .replace("/tmp/chalkcompute-sdk-src", str(tmp_path / "overlay"))
        .replace("/tmp/_archive.tar.gz", str(tmp_path / "archive.tar.gz"))
    )
    result = subprocess.run(
        command,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(installed.parent)},
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "overlay" / "chalk_remote_call").exists()


def test_local_remote_call_wheel_force_reinstalls_before_handler(
    tmp_path,
) -> None:
    wheel = tmp_path / "chalk_remote_call_python-0.0.0-cp311-cp311-linux_x86_64.whl"
    wheel.write_bytes(b"wheel")

    with patch.dict(os.environ, {}, clear=False):
        os.environ[_LOCAL_REMOTE_CALL_WHEEL_ENV_VAR] = str(wheel)
        os.environ.pop(_LOCAL_COPY_ENV_VAR, None)
        os.environ.pop(_LOCAL_WHEEL_ENV_VAR, None)
        os.environ["CHALK_FUNCTION_STRIP_SOURCE"] = "0"
        entrypoint = _local_install_entrypoint(
            ["chalk-remote-call", "--handler", "handler.handler"], None
        )

    assert entrypoint[:2] == ["sh", "-c"]
    assert (
        "python -m pip install --force-reinstall "
        "/tmp/remote-call-wheel/chalk_remote_call_python-0.0.0-cp311-cp311-linux_x86_64.whl"
    ) in entrypoint[2]


def test_local_sdk_wheel_installs_runtime_dependencies_before_overlay(tmp_path) -> None:
    wheel = tmp_path / "chalkcompute-0.0.0-cp314-cp314-linux_x86_64.whl"
    wheel.write_bytes(b"wheel")

    with patch.dict(os.environ, {}, clear=False):
        os.environ[_LOCAL_WHEEL_ENV_VAR] = str(wheel)
        os.environ.pop(_LOCAL_COPY_ENV_VAR, None)
        os.environ["CHALK_FUNCTION_STRIP_SOURCE"] = "0"
        image = _make_base_image(None)
        entrypoint = _local_install_entrypoint(
            ["chalk-remote-call", "--handler", "handler.handler"], image
        )

    packages = image.to_proto().steps[0].step.value.packages
    assert packages[0].startswith("chalk-remote-call-python")
    assert "connectrpc>=0.12.1" in packages
    assert any(lf.src == str(wheel) for lf in image.lazy_local_files)
    assert (
        "python -m pip install --force-reinstall --no-deps "
        "/tmp/sdk/chalkcompute-0.0.0-cp314-cp314-linux_x86_64.whl"
    ) in entrypoint[2]


def test_make_base_image_mounts_local_remote_call_wheel(
    tmp_path,
) -> None:
    wheel = tmp_path / "chalk_remote_call_python-0.0.0-cp311-cp311-linux_x86_64.whl"
    wheel.write_bytes(b"wheel")

    with patch.dict(os.environ, {}, clear=False):
        os.environ[_LOCAL_REMOTE_CALL_WHEEL_ENV_VAR] = str(wheel)
        os.environ.pop(_LOCAL_COPY_ENV_VAR, None)
        os.environ.pop(_LOCAL_WHEEL_ENV_VAR, None)
        os.environ["CHALK_FUNCTION_STRIP_SOURCE"] = "0"
        image = _make_base_image(None)

    assert any(
        lf.src == str(wheel)
        and lf.dest
        == "/tmp/remote-call-wheel/chalk_remote_call_python-0.0.0-cp311-cp311-linux_x86_64.whl"
        for lf in image.lazy_local_files
    )


def test_function_deploy_image_groups_local_mounts_by_purpose(
    tmp_path,
) -> None:
    wheel = tmp_path / "chalk_remote_call_python-0.0.0-cp311-cp311-linux_x86_64.whl"
    wheel.write_bytes(b"wheel")

    def add(x: int, y: int) -> int:
        return x + y

    with patch.dict(os.environ, {}, clear=False):
        os.environ[_LOCAL_COPY_ENV_VAR] = "1"
        os.environ[_LOCAL_REMOTE_CALL_WHEEL_ENV_VAR] = str(wheel)
        os.environ.pop(_LOCAL_WHEEL_ENV_VAR, None)
        os.environ["CHALK_FUNCTION_STRIP_SOURCE"] = "0"
        fn = RemoteFunction(add)
        image = fn._build_deploy_image()

    grouped = image.lazy_local_files_grouped_for_volume_mounts()
    assert set(grouped) == {
        "/app",
        "/app/tests",
        "/tmp/remote-call-wheel",
        "/tmp/sdk",
    }


def test_build_function_runtime_image_uses_standard_local_sdk_base() -> None:
    image = build_function_runtime_image()

    proto = image.to_proto()
    assert proto.base_image.startswith("python:3.11-slim")
    assert proto.steps[0].step.value.packages == ["chalkcompute==0.0.0"]


def test_function_runtime_image_or_none_returns_none_inside_pod() -> None:
    with patch.dict(os.environ, {"CHALK_REMOTE_FUNCTION": "1"}, clear=False):
        assert function_runtime_image_or_none() is None


def test_function_runtime_image_or_none_builds_image_outside_pod() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CHALK_REMOTE_FUNCTION", None)
        image = function_runtime_image_or_none()

    assert image is not None
    proto = image.to_proto()
    assert proto.base_image.startswith("python:3.11-slim")
