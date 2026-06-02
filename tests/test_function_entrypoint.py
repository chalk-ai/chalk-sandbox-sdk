from chalkcompute._function import (
    _LOCAL_COPY_ENV_VAR,
    _LOCAL_WHEEL_ENV_VAR,
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
