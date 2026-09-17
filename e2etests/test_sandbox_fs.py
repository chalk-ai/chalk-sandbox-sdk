"""Native filesystem against a minimal image on a supporting sandbox runtime."""

from __future__ import annotations

import errno
import os

import pytest

from chalkcompute import Image, Sandbox, SandboxClient, SandboxFileSystemError
from e2etests._ci_resources import build_scoped_name
from e2etests.conftest import needs_staging


@needs_staging
def test_native_filesystem_without_image_dependencies(tmp_path):
    image = Image.base("debian:trixie-slim").workdir("/workspace")
    # Allows testing a candidate binary before the runtime image is deployed.
    # Normal CI exercises only the runtime-provided mount.
    candidate = os.environ.get("CHALK_SANDBOX_FS_E2E_BINARY")
    if candidate:
        image = image.add_local_file(
            candidate, "/.chalk/bin/chalk-sandbox-entrypoint", mode=0o755
        )
    with SandboxClient.from_env() as client:
        sandbox = Sandbox(
            image=image,
            name=build_scoped_name("sandbox-native-fs"),
            lifetime="600s",
            sandbox_client=client,
        )
        try:
            sandbox.run(timeout=300)
            for dependency in (
                "/usr/bin/python3",
                "/usr/local/bin/python3",
                "/usr/lib/openssh/sftp-server",
            ):
                assert sandbox.exec("test", "-e", dependency).exit_code == 1
            fs = sandbox.fs
            assert sandbox.get_work_dir() == "/workspace"
            assert sandbox.get_user_home_dir() == "/root"
            fs.create_folder("data/nested")
            payload = bytes(range(256)) * 4096
            source = tmp_path / "source.bin"
            source.write_bytes(payload)
            fs.upload_file(source, "data/nested/file.bin")
            destination = tmp_path / "download.bin"
            assert fs.download_file("data/nested/file.bin", destination) is None
            assert destination.read_bytes() == payload
            assert fs.get_file_info("data/nested/file.bin").size == len(payload)
            assert fs.search_files("data", "*.bin") == ["data/nested/file.bin"]
            fs.set_file_permissions("data/nested/file.bin", mode="600")
            fs.write_file("data/nested/file.bin", b"existing")

            def interrupted():
                yield b"partial"
                raise RuntimeError("upload source failed")

            with pytest.raises(RuntimeError, match="upload source failed"):
                fs.upload_file_stream(interrupted(), "data/nested/file.bin")
            assert fs.read_file("data/nested/file.bin") == b"existing"
            assert fs.get_file_info("data/nested/file.bin").permissions == "600"
            fs.write_file("data/notes.txt", "hello\r\nworld\r\n")
            assert fs.find_files("data", "world")[0].line_number == 2
            assert fs.replace_in_files(["data/notes.txt"], "world", "sandbox") == {
                "data/notes.txt": 1
            }
            assert fs.read_file("data/notes.txt") == b"hello\r\nsandbox\r\n"
            with pytest.raises(SandboxFileSystemError) as error:
                fs.read_file("missing")
            assert error.value.errno == errno.ENOENT
            with pytest.raises(SandboxFileSystemError):
                fs.delete_file("data")
            fs.delete_file("data", recursive=True)
            assert fs.list_files() == []
        finally:
            if sandbox.id is not None:
                sandbox.terminate()
