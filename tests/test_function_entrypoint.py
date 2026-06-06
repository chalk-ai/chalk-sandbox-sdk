from chalkcompute._function import (
    RemoteFunction,
    _LOCAL_COPY_ENV_VAR,
    _LOCAL_REMOTE_CALL_WHEEL_ENV_VAR,
    _LOCAL_WHEEL_ENV_VAR,
    _make_base_image,
    _local_install_entrypoint,
)


def test_local_sdk_copy_extracts_archive_to_local_tmp(
    monkeypatch,
) -> None:
    monkeypatch.setenv(_LOCAL_COPY_ENV_VAR, "1")
    monkeypatch.delenv(_LOCAL_WHEEL_ENV_VAR, raising=False)
    monkeypatch.setenv("CHALK_FUNCTION_STRIP_SOURCE", "0")

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
    monkeypatch,
    tmp_path,
) -> None:
    wheel = tmp_path / "chalk_remote_call_python-0.0.0-cp311-cp311-linux_x86_64.whl"
    wheel.write_bytes(b"wheel")

    monkeypatch.setenv(_LOCAL_REMOTE_CALL_WHEEL_ENV_VAR, str(wheel))
    monkeypatch.delenv(_LOCAL_COPY_ENV_VAR, raising=False)
    monkeypatch.delenv(_LOCAL_WHEEL_ENV_VAR, raising=False)
    monkeypatch.setenv("CHALK_FUNCTION_STRIP_SOURCE", "0")

    entrypoint = _local_install_entrypoint(
        ["chalk-remote-call", "--handler", "handler.handler"]
    )

    assert entrypoint[:2] == ["sh", "-c"]
    assert (
        "python -m pip install --force-reinstall "
        "/tmp/remote-call-wheel/chalk_remote_call_python-0.0.0-cp311-cp311-linux_x86_64.whl"
    ) in entrypoint[2]


def test_make_base_image_mounts_local_remote_call_wheel(
    monkeypatch,
    tmp_path,
) -> None:
    wheel = tmp_path / "chalk_remote_call_python-0.0.0-cp311-cp311-linux_x86_64.whl"
    wheel.write_bytes(b"wheel")

    monkeypatch.setenv(_LOCAL_REMOTE_CALL_WHEEL_ENV_VAR, str(wheel))
    monkeypatch.delenv(_LOCAL_COPY_ENV_VAR, raising=False)
    monkeypatch.delenv(_LOCAL_WHEEL_ENV_VAR, raising=False)
    monkeypatch.setenv("CHALK_FUNCTION_STRIP_SOURCE", "0")

    image = _make_base_image(None)

    assert any(
        lf.src == str(wheel)
        and lf.dest
        == "/tmp/remote-call-wheel/chalk_remote_call_python-0.0.0-cp311-cp311-linux_x86_64.whl"
        for lf in image.lazy_local_files
    )


def test_function_deploy_image_groups_local_mounts_by_purpose(
    monkeypatch,
    tmp_path,
) -> None:
    wheel = tmp_path / "chalk_remote_call_python-0.0.0-cp311-cp311-linux_x86_64.whl"
    wheel.write_bytes(b"wheel")

    monkeypatch.setenv(_LOCAL_COPY_ENV_VAR, "1")
    monkeypatch.setenv(_LOCAL_REMOTE_CALL_WHEEL_ENV_VAR, str(wheel))
    monkeypatch.delenv(_LOCAL_WHEEL_ENV_VAR, raising=False)
    monkeypatch.setenv("CHALK_FUNCTION_STRIP_SOURCE", "0")

    def add(x: int, y: int) -> int:
        return x + y

    fn = RemoteFunction(add)
    image = fn._build_deploy_image()

    grouped = image.lazy_local_files_grouped_for_volume_mounts()
    assert set(grouped) == {
        "/app",
        "/tmp/remote-call-wheel",
        "/tmp/sdk",
    }
