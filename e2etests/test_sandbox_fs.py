from __future__ import annotations

import errno
from pathlib import Path

import pytest

from chalkcompute import (
    ComputeClass,
    FilesystemError,
    Sandbox,
    SandboxClient,
    SandboxFileSystemError,
)
from e2etests._ci_resources import build_scoped_name
from e2etests.conftest import needs_staging


@needs_staging
def test_sandbox_filesystem(tmp_path: Path) -> None:
    with SandboxClient.from_env() as client:
        sandbox = Sandbox(
            name=build_scoped_name("sandbox-filesystem"),
            compute_class=ComputeClass.HOST,
            lifetime="600s",
            sandbox_client=client,
        )
        try:
            sandbox.run(timeout=300)

            prefix = "/tmp/chalk-filesystem-e2e"
            data_path = prefix + ".bin"
            empty_path = prefix + ".empty"
            executable_path = prefix + ".sh"
            local_upload_path = prefix + ".local-upload"

            pattern = bytes(range(251))
            size = 2 * 1024 * 1024 + 333
            payload = (pattern * (size // len(pattern) + 1))[:size]

            sandbox.fs.write_bytes(data_path, b"old contents")
            sandbox.fs.upload_iter(
                [
                    payload[: 1024 * 1024],
                    payload[1024 * 1024 : 1024 * 1024 + 17],
                    payload[1024 * 1024 + 17 :],
                ],
                data_path,
                mode=0o640,
            )
            assert sandbox.fs.read_bytes(data_path) == payload
            assert b"".join(sandbox.fs.download_iter(data_path)) == payload

            sandbox.fs.write_bytes(empty_path, b"")
            assert sandbox.fs.read_bytes(empty_path) == b""

            sandbox.fs.write_bytes(
                executable_path,
                b"#!/bin/sh\nprintf filesystem-mode-ok\n",
                mode=0o751,
            )
            execution = sandbox.exec(executable_path)
            assert execution.exit_code == 0
            assert execution.stdout == b"filesystem-mode-ok"

            local_source = tmp_path / "upload.bin"
            local_destination = tmp_path / "download.bin"
            local_source.write_bytes(payload)
            sandbox.fs.upload_file(local_source, local_upload_path)
            sandbox.fs.download_file(local_upload_path, local_destination)
            assert local_destination.read_bytes() == payload

            with pytest.raises(SandboxFileSystemError) as missing:
                sandbox.fs.read_bytes(prefix + ".missing")
            assert missing.value.error is FilesystemError.NOT_FOUND
            assert missing.value.errno == errno.ENOENT

            with pytest.raises(SandboxFileSystemError) as directory:
                sandbox.fs.write_bytes("/tmp", b"not a directory")
            assert directory.value.error is FilesystemError.FAILED_PRECONDITION
            assert directory.value.errno == errno.EISDIR
        finally:
            if sandbox.id is not None:
                sandbox.terminate()
