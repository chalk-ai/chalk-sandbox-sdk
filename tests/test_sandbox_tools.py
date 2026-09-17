"""Exercise sandbox helpers with real Git/Python processes, without cloud access."""

from __future__ import annotations

import os
import subprocess
import sys
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
        self.command, self.cwd, self.env = command, cwd, env
        self.data = bytearray()

    def write_stdin(self, data):
        self.data.extend(data)

    def close_stdin(self):
        pass

    def wait(self, *, auto_reattach=True):
        result = subprocess.run(
            self.command,
            input=bytes(self.data),
            capture_output=True,
            cwd=self.cwd,
            env=self.env,
            timeout=20,
        )
        return ExecResult(result.stdout, result.stderr, result.returncode)


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

    def start(command, *args, workdir=None, env=None, timeout_secs=None):
        if command == "python3":
            command = sys.executable
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
    sandbox.git.clone(url, "clone")
    assert sandbox.git.status("clone").current_branch == "trunk"
    assert sandbox.git.status("clone").branch_published
    assert (
        sandbox.exec(
            "git", "rev-list", "--count", "HEAD", workdir="clone"
        ).stdout_text.strip()
        == "2"
    )
    sandbox.git.clone(url, "shallow", depth=1, branch="trunk")
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
    assert sandbox.fs.download_file(path, local) == data
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
    assert sandbox.get_user_home_dir() == str(tmp_path)


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
    sandbox.fs.read_file("file", timeout_secs=13)
    assert sandbox.exec.call_args.kwargs["timeout_secs"] == 13
    sandbox.exec.return_value = ExecResult(b"", b"", None, signal=9)
    with pytest.raises(GitError):
        sandbox.git.status("repo")
    with pytest.raises(SandboxFileSystemError):
        sandbox.fs.read_file("file")


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


def test_upload_does_not_reattach_without_replaying_stdin():
    sandbox = Sandbox(sandbox_id="test")
    process = MagicMock()
    process.wait.return_value = ExecResult(b"", b"", 0)
    sandbox.exec_start = MagicMock(return_value=process)
    sandbox.fs.write_file("empty", b"")
    process.close_stdin.assert_called_once()
    process.wait.assert_called_once_with(auto_reattach=False)


def test_download_retries_stream_resets_without_returning_partial_data():
    sandbox = Sandbox(sandbox_id="test")
    reset = ExecResult(b"partial", b"", None, error="hypervisor_error: RST_STREAM")
    sandbox.exec = MagicMock(side_effect=[reset, ExecResult(b"complete", b"", 0)])
    with patch("chalkcompute._sandbox_fs.time.sleep"):
        assert sandbox.fs.read_file("file") == b"complete"
    assert sandbox.exec.call_count == 2
    sandbox.exec = MagicMock(return_value=reset)
    with (
        patch("chalkcompute._sandbox_fs.time.sleep"),
        pytest.raises(SandboxFileSystemError),
    ):
        sandbox.fs.read_file("file")
    assert sandbox.exec.call_count == 3
    sandbox.exec.reset_mock()
    with pytest.raises(SandboxFileSystemError):
        sandbox.fs.delete_file("file")
    assert sandbox.exec.call_count == 1


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
