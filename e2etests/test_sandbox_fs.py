from __future__ import annotations

import errno
from pathlib import Path

import pytest

from chalkcompute import (
    ComputeClass,
    FileMatch,
    FilesystemError,
    FileUpload,
    ReplaceResult,
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


@needs_staging
def test_sandbox_filesystem_operations(tmp_path: Path) -> None:
    local_script = tmp_path / "script.sh"
    local_script.write_text("#!/bin/sh\nprintf queued-ok\n")
    with SandboxClient.from_env() as client:
        sandbox = Sandbox(
            name=build_scoped_name("sandbox-filesystem-ops"),
            compute_class=ComputeClass.HOST,
            lifetime="600s",
            sandbox_client=client,
        )
        root = "/tmp/chalk-filesystem-ops"
        sandbox.fs.add_file(local_script, root + "/queued/run.sh", mode=0o755)
        sandbox.fs.add_file(b"queued bytes\n", root + "/queued/data.txt")
        try:
            sandbox.run(timeout=300)

            assert sandbox.exec(root + "/queued/run.sh").stdout == b"queued-ok"
            assert sandbox.fs.read_bytes(root + "/queued/data.txt") == b"queued bytes\n"

            sandbox.fs.create_folder(root + "/src/pkg", 0o700)
            sandbox.fs.upload_files(
                [
                    FileUpload(b"# TODO: first\nvalue = 1\n", root + "/src/pkg/mod.py"),
                    FileUpload(b"nothing here\n", root + "/src/notes.txt", 0o640),
                ]
            )
            sandbox.exec("ln", "-s", "notes.txt", root + "/src/link")

            listing = sandbox.fs.list_files(root + "/src")
            assert [info.name for info in listing] == ["link", "notes.txt", "pkg"]
            link, notes, pkg = listing
            assert link.is_symlink and link.symlink_target == "notes.txt"
            assert notes.size == 13 and notes.mode == 0o640 and not notes.is_dir
            assert pkg.is_dir and pkg.mode == 0o700
            assert sandbox.fs.get_file_info(root + "/src/pkg/mod.py").size == 24

            assert sandbox.fs.search_files(root, "*.py") == [root + "/src/pkg/mod.py"]
            assert sandbox.fs.find_files(root + "/src", "TODO") == [
                FileMatch(root + "/src/pkg/mod.py", 1, "# TODO: first")
            ]
            assert (
                sandbox.fs.find_files(root + "/src", "^value = [0-9]$", regex=True)[
                    0
                ].line
                == 2
            )
            assert sandbox.fs.find_files(root + "/src", "absent") == []

            replaced, missing_result = sandbox.fs.replace_in_files(
                [root + "/src/pkg/mod.py", root + "/src/missing"],
                "value = 1",
                "value = 2",
            )
            assert replaced == ReplaceResult(root + "/src/pkg/mod.py", True, 1)
            assert not missing_result.success and missing_result.error
            assert b"value = 2" in sandbox.fs.download_file(root + "/src/pkg/mod.py")

            sandbox.fs.set_file_permissions(root + "/src/notes.txt", mode=0o600)
            assert sandbox.fs.get_file_info(root + "/src/notes.txt").mode == 0o600
            sandbox.fs.move_files(root + "/src/notes.txt", root + "/src/renamed.txt")
            sandbox.fs.delete_file(root + "/src/link")
            with pytest.raises(SandboxFileSystemError) as not_empty:
                sandbox.fs.delete_file(root + "/src/pkg")
            assert not_empty.value.error is FilesystemError.FAILED_PRECONDITION
            sandbox.fs.delete_file(root + "/src/pkg", recursive=True)
            assert [info.name for info in sandbox.fs.list_files(root + "/src")] == [
                "renamed.txt"
            ]
            with pytest.raises(SandboxFileSystemError) as missing:
                sandbox.fs.get_file_info(root + "/src/pkg")
            assert missing.value.error is FilesystemError.NOT_FOUND

            sandbox.fs.add_file(b"late", root + "/late.txt")
            assert sandbox.fs.read_bytes(root + "/late.txt") == b"late"
        finally:
            if sandbox.id is not None:
                sandbox.terminate()
