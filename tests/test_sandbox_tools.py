"""Exercise sandbox helpers with real Git/SFTP processes, without cloud access."""

from __future__ import annotations

import os
import subprocess
import errno
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from chalkcompute import (
    ExecResult,
    GitError,
    Image,
    Sandbox,
    SandboxClient,
    SandboxError,
    SandboxFileSystemError,
)


class LocalProcess:
    def __init__(self, command, *, cwd, env):
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
        )

    def write_stdin(self, data):
        self.process.stdin.write(data)
        self.process.stdin.flush()

    def close_stdin(self):
        if self.process.stdin is not None:
            self.process.stdin.close()
            self.process.stdin = None

    def wait(self, *, auto_reattach=True):
        stdout, stderr = self.process.communicate(timeout=20)
        return ExecResult(stdout, stderr, self.process.returncode)

    def stdout_bytes(self):
        while chunk := self.process.stdout.read1(32768):
            yield chunk
        stderr = self.process.stderr.read()
        if self.process.wait(timeout=20):
            raise SandboxError(stderr.decode())


@pytest.fixture
def sandbox(tmp_path):
    handle = Sandbox(sandbox_id="local-test")
    # Isolate Git from developer/global config and hooks.
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "HOME": str(tmp_path),
    }

    server = next(
        (
            path
            for path in (
                "/usr/libexec/sftp-server",
                "/usr/lib/openssh/sftp-server",
                "/usr/lib/ssh/sftp-server",
            )
            if Path(path).exists()
        ),
        None,
    )
    if server is None:
        pytest.skip("OpenSSH sftp-server is required for local native filesystem tests")

    def start(command, *args, workdir=None, env=None, timeout_secs=None):
        assert command not in ("python", "python3", "sh", "bash")
        if command == "/usr/lib/openssh/sftp-server":
            command = server
        cwd = tmp_path / workdir if workdir else tmp_path
        return LocalProcess([command, *args], cwd=cwd, env={**base_env, **(env or {})})

    base_env = env
    handle.exec_start = start
    return handle


def seed(sandbox, path="repo"):
    sandbox.git.init(path, initial_branch="trunk")
    sandbox.fs.write_file(f"{path}/initial.txt", "initial\n")
    sandbox.git.add(path, ["initial.txt"])
    return sandbox.git.commit(path, "initial", "Test Author", "test@example.com")


def test_git_lifecycle(sandbox):
    first = seed(sandbox)
    assert len(first) == 40
    assert sandbox.git.status("repo").current_branch == "trunk"
    assert sandbox.git.status("repo").file_status == []
    sandbox.git.create_branch("repo", "feature")
    assert sandbox.git.status("repo").current_branch == "trunk"
    sandbox.git.checkout_branch("repo", "feature")
    assert sandbox.git.branches("repo") == ["feature", "trunk"]
    unusual = "space ' ; $file\nname.txt"
    sandbox.fs.write_file("repo/" + unusual, "hello")
    status = sandbox.git.status("repo")
    assert status.file_status[0].name == unusual
    assert status.file_status[0].index == "?"
    sandbox.git.add("repo", [unusual])
    assert sandbox.git.status("repo").file_status[0].index == "A"
    sandbox.git.reset("repo", files=[unusual])
    assert sandbox.git.status("repo").file_status[0].index == "?"
    sandbox.git.add("repo", [unusual])
    sandbox.git.commit("repo", "add unusual path", "Test", "test@example.com")
    sandbox.fs.move_files("repo/" + unusual, "repo/renamed.txt")
    sandbox.git.add("repo", ["."])
    status = sandbox.git.status("repo")
    assert status.file_status[0].index == "R"
    assert status.file_status[0].original_name == unusual
    sandbox.git.restore("repo", [unusual, "renamed.txt"], staged=True)
    sandbox.git.reset("repo", mode="hard", target=first)
    sandbox.git.checkout_branch("repo", "trunk")
    sandbox.git.delete_branch("repo", "feature")
    assert sandbox.git.branches("repo") == ["trunk"]
    with pytest.raises(GitError):
        sandbox.git.commit("repo", "empty", "Test", "test@example.com")
    sandbox.git.commit("repo", "empty", "Test", "test@example.com", allow_empty=True)


def test_clone_defaults_shallow_and_detached(sandbox, tmp_path):
    first = seed(sandbox, "origin")
    sandbox.fs.write_file("origin/initial.txt", "second")
    sandbox.git.add("origin", ["."])
    sandbox.git.commit("origin", "second", "Test", "test@example.com")
    url = (tmp_path / "origin").as_uri()
    sandbox.git.clone(url, "clone", depth=None)
    assert sandbox.git.status("clone").current_branch == "trunk"
    assert sandbox.git.status("clone").branch_published
    assert (
        sandbox.exec(
            "git", "rev-list", "--count", "HEAD", workdir="clone"
        ).stdout_text.strip()
        == "2"
    )
    sandbox.git.clone(url, "shallow")
    assert (
        sandbox.exec(
            "git", "rev-list", "--count", "HEAD", workdir="shallow"
        ).stdout_text.strip()
        == "1"
    )
    sandbox.git.clone(url, "detached", commit_id=first)
    assert sandbox.git.status("detached").current_branch is None
    assert sandbox.fs.read_file("detached/initial.txt") == b"initial\n"


def test_remotes_push_pull_fetch_config(sandbox, tmp_path):
    seed(sandbox)
    sandbox.git.init("remote", bare=True, initial_branch="trunk")
    url = str(tmp_path / "remote")
    sandbox.git.remote_add("repo", "origin", url)
    assert sandbox.git.remote_get("repo", "origin").fetch_url == url
    assert sandbox.git.remotes("repo")[0].push_url == url
    sandbox.git.push("repo", set_upstream=True)
    sandbox.git.clone(url, "other")
    sandbox.fs.write_file("repo/initial.txt", "changed")
    sandbox.git.add("repo", ["."])
    sandbox.git.commit("repo", "change", "Test", "test@example.com")
    assert sandbox.git.status("repo").ahead == 1
    sandbox.git.push("repo")
    sandbox.git.fetch("other")
    assert sandbox.git.status("other").behind == 1
    sandbox.git.pull("other")
    assert sandbox.fs.read_file("other/initial.txt") == b"changed"
    sandbox.git.remote_add("other", "origin", url, overwrite=True, fetch=True)
    sandbox.git.configure_user("other", "Local User", "local@example.com")
    assert sandbox.git.get_config("other", "user.name") == "Local User"
    with pytest.raises(GitError):
        sandbox.git.get_config("other", "missing.key")
    # Per-commit author identity did not change repository configuration.
    with pytest.raises(GitError):
        sandbox.git.get_config("repo", "user.name")


def test_pull_does_not_merge_diverged_branches(sandbox, tmp_path):
    seed(sandbox)
    sandbox.git.clone(str(tmp_path / "repo"), "other")
    sandbox.git.commit("repo", "one", "Test", "test@example.com", allow_empty=True)
    sandbox.git.commit("other", "two", "Test", "test@example.com", allow_empty=True)
    with pytest.raises(GitError, match="fast-forward"):
        sandbox.git.pull("other")


def test_credentials_are_command_scoped_and_redacted(sandbox, tmp_path):
    seed(sandbox)
    real_exec = sandbox.exec
    calls = []

    def capture(*args, **kwargs):
        calls.append((args, kwargs))
        return real_exec(*args, **kwargs)

    sandbox.exec = capture
    password = 'token $() ; " special'
    sandbox.git.clone(
        str(tmp_path / "repo"), "clone", username="user", password=password
    )
    args, kwargs = calls[-1]
    assert password not in repr(args)
    assert kwargs["env"]["CHALK_GIT_PASSWORD"] == password
    assert password not in sandbox.fs.read_file("clone/.git/config").decode()
    # Ask Git itself to invoke the helper, including shell metacharacters.
    prefix = args[: args.index("clone")]
    process = sandbox.exec_start(*prefix, "credential", "fill", env=kwargs["env"])
    process.write_stdin(b"protocol=https\nhost=example.com\n\n")
    process.close_stdin()
    assert f"password={password}" in process.wait().stdout_text
    sandbox.exec = MagicMock(return_value=ExecResult(b"", password.encode(), 1))
    with pytest.raises(GitError) as error:
        sandbox.git.pull("repo", username="user", password=password)
    assert password not in str(error.value)


@pytest.mark.parametrize(
    "operation",
    [
        lambda git: git.clone("url", "path", depth=0),
        lambda git: git.clone("url", "path", depth=True),
        lambda git: git.clone("url", "path", commit_id="--help"),
        lambda git: git.clone("url", "path", username="user"),
        lambda git: git.clone("url", "path", username="user", password="bad\npassword"),
        lambda git: git.checkout_branch("repo", "--help"),
        lambda git: git.push("repo", remote="--help"),
        lambda git: git.add("repo", []),
        lambda git: git.reset("repo", mode="hard", files=["file"]),
        lambda git: git.reset("repo", mode="oops"),
    ],
)
def test_git_invalid_arguments_do_not_execute(operation):
    sandbox = Sandbox(sandbox_id="test")
    sandbox.exec = MagicMock()
    with pytest.raises(ValueError):
        operation(sandbox.git)
    sandbox.exec.assert_not_called()


def test_files_binary_paths_and_metadata(sandbox, tmp_path):
    sandbox.fs.create_folder("dir", "700")
    path = "dir/ - ' ; $(touch bad)\nfile.bin"
    data = bytes(range(256)) * 1024
    sandbox.fs.write_file(path, data)
    assert sandbox.fs.read_file(path) == data
    assert sandbox.fs.get_file_info(path).size == len(data)
    assert sandbox.fs.list_files("dir")[0].name == Path(path).name
    assert not (tmp_path / "bad").exists()
    sandbox.fs.set_file_permissions(path, mode="600")
    assert sandbox.fs.get_file_info(path).permissions == "600"
    local = tmp_path / "download"
    assert sandbox.fs.download_file(path, local) is None
    assert local.read_bytes() == data
    sandbox.fs.upload_file(local, "dir/upload")
    assert sandbox.fs.read_file("dir/upload") == data
    sandbox.fs.upload_file(b"", "dir/empty")
    assert sandbox.fs.read_file("dir/empty") == b""
    sandbox.fs.move_files("dir/empty", "dir/moved")
    with pytest.raises(SandboxFileSystemError):
        sandbox.fs.delete_file("dir")
    sandbox.fs.delete_file("dir", recursive=True)
    assert not (tmp_path / "dir").exists()
    assert sandbox.get_work_dir() == str(tmp_path)
    import pwd

    assert sandbox.get_user_home_dir() == pwd.getpwuid(os.getuid()).pw_dir


def test_find_replace_symlinks_errors(sandbox, tmp_path):
    sandbox.fs.create_folder("dir/nested")
    sandbox.fs.write_file("dir/nested/a.txt", "hello\nhello world\n")
    sandbox.fs.write_file("dir/binary", b"\xff\x00")
    (tmp_path / "outside.txt").write_text("hello")
    (tmp_path / "dir/link").symlink_to(tmp_path / "outside.txt")
    assert sandbox.fs.get_file_info("dir/link").is_symlink
    assert sandbox.fs.search_files("dir", "*.txt") == ["dir/nested/a.txt"]
    matches = sandbox.fs.find_files("dir", "hello")
    assert [m.line_number for m in matches] == [1, 2]
    assert sandbox.fs.replace_in_files(["dir/nested/a.txt"], "hello", "bye") == {
        "dir/nested/a.txt": 2
    }
    assert sandbox.fs.read_file("dir/nested/a.txt") == b"bye\nbye world\n"
    sandbox.fs.delete_file("dir/link", recursive=True)
    assert (tmp_path / "outside.txt").exists()
    with pytest.raises(SandboxFileSystemError):
        sandbox.fs.read_file("missing")
    with pytest.raises(SandboxFileSystemError):
        sandbox.fs.search_files("missing", "*")
    with pytest.raises(ValueError):
        sandbox.fs.create_folder("bad", "999")


def test_helpers_forward_timeout_and_fail_on_signal():
    sandbox = Sandbox(sandbox_id="test")
    sandbox.exec = MagicMock(return_value=ExecResult(b"", b"", 0))
    sandbox.git.clone("https://example.com/repo", "repo", timeout_secs=12)
    assert sandbox.exec.call_args.kwargs["timeout_secs"] == 12
    sandbox.exec.return_value = ExecResult(b"", b"", None, signal=9)
    with pytest.raises(GitError):
        sandbox.git.status("repo")


def test_helpers_require_a_running_sandbox():
    sandbox = Sandbox()
    with pytest.raises(SandboxError, match="not running"):
        sandbox.git.init("repo")
    with pytest.raises(SandboxError, match="not running"):
        sandbox.fs.read_file("file")


def test_default_image_for_both_creation_paths():
    expected = Image.sandbox().to_proto()
    assert Sandbox()._image.to_proto() == expected
    assert Sandbox()._image is not Sandbox()._image
    client = SandboxClient()
    try:
        with patch.object(
            Sandbox, "run", autospec=True, side_effect=lambda self, **kwargs: self
        ):
            sandbox = client.create()
        assert sandbox._image.to_proto() == expected
        assert (
            sandbox._cpu is None
            and sandbox._memory is None
            and sandbox._lifetime is None
        )
        assert Sandbox(image="custom")._image == "custom"
        assert Sandbox(sandbox_id="existing")._image is None
    finally:
        client.close()


def test_text_replacement_preserves_newlines(sandbox):
    sandbox.fs.write_file("crlf.txt", b"one\r\ntwo\r\n")
    sandbox.fs.replace_in_files(["crlf.txt"], "two", "three")
    assert sandbox.fs.read_file("crlf.txt") == b"one\r\nthree\r\n"


def test_upload_failure_preserves_destination_and_removes_temporary_file(
    sandbox, tmp_path
):
    sandbox.fs.write_file("destination", b"original")

    def broken_chunks():
        yield b"partial"
        raise RuntimeError("producer failed")

    with pytest.raises(RuntimeError, match="producer failed"):
        sandbox.fs.upload_file_stream(broken_chunks(), "destination")
    assert sandbox.fs.read_file("destination") == b"original"
    assert not list(tmp_path.glob(".chalk-upload-*"))


def test_download_failure_preserves_local_destination(sandbox, tmp_path):
    destination = tmp_path / "destination"
    destination.write_bytes(b"original")

    def broken_download(*args, **kwargs):
        yield b"partial"
        raise SandboxFileSystemError(errno.EIO, "stream disconnected")

    fs = sandbox.fs
    with patch.object(fs, "download_file_stream", broken_download):
        with pytest.raises(SandboxFileSystemError):
            fs.download_file("remote", destination)
    assert destination.read_bytes() == b"original"
    assert not list(tmp_path.glob(".chalk-download-*"))


def test_streams_are_bounded_and_writes_preserve_permissions(sandbox, tmp_path):
    sandbox.fs.upload_file_stream((b"a" * 4096 for _ in range(100)), "large")
    chunks = list(sandbox.fs.download_file_stream("large"))
    assert max(map(len, chunks)) <= 32768
    assert b"".join(chunks) == b"a" * 409600
    sandbox.fs.set_file_permissions("large", mode="640")
    sandbox.fs.write_file("large", "replacement")
    assert sandbox.fs.get_file_info("large").permissions == "640"
    assert sandbox.fs.read_text("large") == "replacement"
    (tmp_path / "link").symlink_to(tmp_path / "large")
    with pytest.raises(SandboxFileSystemError) as error:
        sandbox.fs.write_file("link", "bad")
    assert error.value.errno == errno.ELOOP
    assert sandbox.fs.read_text("large") == "replacement"
    with pytest.raises(SandboxFileSystemError) as error:
        sandbox.fs.read_file("missing")
    assert error.value.errno == errno.ENOENT


def test_git_relative_paths_use_git_c_instead_of_exec_workdir():
    sandbox = Sandbox(sandbox_id="test")
    sandbox.exec = MagicMock(return_value=ExecResult(b"", b"", 0))
    sandbox.git.add("relative/repo", ["file"])
    assert sandbox.exec.call_args.args == (
        "git",
        "-C",
        "relative/repo",
        "add",
        "--",
        "file",
    )
    assert "workdir" not in sandbox.exec.call_args.kwargs


def test_sftp_channel_handles_fragmented_and_empty_frames():
    from chalkcompute._sandbox_fs import _ExecSocket

    process = MagicMock()
    process.stdout_bytes.return_value = iter([b"", b"ab", b"cdef"])
    channel = _ExecSocket(process)
    assert channel.recv(1) == b"a"
    assert channel.recv(8) == b"b"
    assert channel.recv(8) == b"cdef"
    assert channel.recv(8) == b""
    channel.close()
    channel.close()
    process.close_stdin.assert_called_once()


def test_download_iterator_can_be_closed_early(sandbox):
    sandbox.fs.write_file("large", b"a" * 100000)
    stream = sandbox.fs.download_file_stream("large")
    assert len(next(stream)) == 32768
    stream.close()
    assert sandbox.fs.read_file("large") == b"a" * 100000
