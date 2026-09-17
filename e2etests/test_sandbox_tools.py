"""Git/files in the default sandbox image and opt-in tmux image support."""

from __future__ import annotations

from chalkcompute import Sandbox, SandboxClient
from e2etests._ci_resources import build_scoped_name
from e2etests.conftest import needs_staging


@needs_staging
def test_default_sandbox_git_and_files():
    with SandboxClient.from_env() as client:
        sandbox = Sandbox(
            name=build_scoped_name("sandbox-tools"),
            lifetime="600s",
            sandbox_client=client,
        )
        try:
            sandbox.run(timeout=300)
            assert sandbox.get_work_dir() == "/workspace"
            sandbox.git.init("repo", initial_branch="trunk")
            payload = bytes(range(256)) * 1024
            sandbox.fs.upload_file(payload, "repo/binary file")
            assert sandbox.fs.download_file("repo/binary file") == payload
            sandbox.fs.write_file("repo/README.md", "hello sandbox\n")
            sandbox.git.add("repo", ["."])
            commit = sandbox.git.commit(
                "repo", "initial", "SDK Test", "sdk@example.com"
            )
            assert len(commit) == 40
            sandbox.git.clone("file:///workspace/repo", "clone")
            status = sandbox.git.status("clone")
            assert status.current_branch == "trunk"
            assert status.file_status == []
            assert sandbox.fs.read_file("clone/binary file") == payload
            assert (
                sandbox.fs.find_files("clone", "hello sandbox")[0].path
                == "clone/README.md"
            )
        finally:
            if sandbox.id is not None:
                sandbox.terminate()


@needs_staging
def test_sandbox_tmux_image():
    from chalkcompute import Image

    with SandboxClient.from_env() as client:
        sandbox = Sandbox(
            image=Image.sandbox().apt_install(["tmux"]),
            name=build_scoped_name("sandbox-tmux"),
            lifetime="600s",
            sandbox_client=client,
        )
        try:
            sandbox.run(timeout=300)
            version = sandbox.exec("tmux", "-V")
            assert version.exit_code == 0, version.stderr_text
            assert version.stdout_text.startswith("tmux ")
            for args in [
                ("new-session", "-d", "-s", "work"),
                ("has-session", "-t", "work"),
                (
                    "send-keys",
                    "-t",
                    "work",
                    "printf tmux-ok > /workspace/tmux-output; tmux wait-for -S ready",
                    "Enter",
                ),
                ("wait-for", "ready"),
            ]:
                result = sandbox.exec("tmux", *args, timeout_secs=10)
                assert result.exit_code == 0, result.error or result.stderr_text
            assert sandbox.fs.read_file("/workspace/tmux-output") == b"tmux-ok"
            capture = sandbox.exec("tmux", "capture-pane", "-p", "-t", "work")
            assert capture.exit_code == 0, capture.stderr_text
            assert "tmux-ok" in capture.stdout_text
            assert sandbox.exec("tmux", "kill-session", "-t", "work").exit_code == 0
            assert sandbox.exec("tmux", "has-session", "-t", "work").exit_code != 0
        finally:
            if sandbox.id is not None:
                sandbox.terminate()


@needs_staging
def test_native_filesystem_without_python(tmp_path):
    import errno

    import pytest

    from chalkcompute import Image, SandboxFileSystemError

    with SandboxClient.from_env() as client:
        sandbox = Sandbox(
            image=Image.base("debian:trixie-slim")
            .apt_install(["openssh-sftp-server"])
            .workdir("/workspace"),
            name=build_scoped_name("sandbox-native-fs"),
            lifetime="600s",
            sandbox_client=client,
        )
        try:
            sandbox.run(timeout=300)
            for interpreter in ("/usr/bin/python3", "/usr/local/bin/python3"):
                assert sandbox.exec("test", "-e", interpreter).exit_code == 1
            fs = sandbox.fs
            fs.create_folder("data/nested")
            data = bytes(range(256)) * 4096
            local = tmp_path / "source.bin"
            local.write_bytes(data)
            fs.upload_file(local, "data/nested/file.bin")
            downloaded = tmp_path / "download.bin"
            assert fs.download_file("data/nested/file.bin", downloaded) is None
            assert downloaded.read_bytes() == data
            assert fs.get_file_info("data/nested/file.bin").size == len(data)
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
