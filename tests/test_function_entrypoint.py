import os
from unittest.mock import patch

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
            ["chalk-remote-call", "--handler", "handler.handler"]
        )

    assert entrypoint[:2] == ["sh", "-c"]
    command = entrypoint[2]
    assert "cat /tmp/sdk/_archive.part.* > /tmp/_archive.tar.gz" in command
    assert "tar xzf /tmp/_archive.tar.gz -C /tmp/chalkcompute-sdk-src" in command
    assert "export PYTHONPATH=/tmp/chalkcompute-sdk-src:$PYTHONPATH" in command
    assert "python -m pip install /tmp/chalkcompute-sdk-src" not in command
    assert "tar xzf /tmp/sdk/_archive.tar.gz -C /tmp/sdk" not in command


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
            ["chalk-remote-call", "--handler", "handler.handler"]
        )

    assert entrypoint[:2] == ["sh", "-c"]
    assert (
        "python -m pip install --force-reinstall "
        "/tmp/remote-call-wheel/chalk_remote_call_python-0.0.0-cp311-cp311-linux_x86_64.whl"
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
        "/tmp/remote-call-wheel",
        "/tmp/sdk",
    }


def test_build_function_runtime_image_uses_standard_local_sdk_base() -> None:
    image = build_function_runtime_image()

    proto = image.to_proto()
    assert proto.base_image.startswith("python:3.11-slim")
    assert proto.steps[0].pip_install.packages == ["chalkcompute==0.0.0"]


def test_function_runtime_image_or_none_returns_none_inside_pod() -> None:
    with patch.dict(os.environ, {"_CHALK_REMOTE_FUNCTION": "1"}, clear=False):
        assert function_runtime_image_or_none() is None


def test_function_runtime_image_or_none_builds_image_outside_pod() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("_CHALK_REMOTE_FUNCTION", None)
        image = function_runtime_image_or_none()

    assert image is not None
    proto = image.to_proto()
    assert proto.base_image.startswith("python:3.11-slim")
