"""Exercise sandbox helpers with real Git processes, without cloud access."""

from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from chalkcompute import (
    ExecResult,
    GitError,
    Image,
    Sandbox,
    SandboxClient,
    SandboxError,
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
        assert command not in ("python", "python3", "sh", "bash")
        cwd = tmp_path / workdir if workdir else tmp_path
        return LocalProcess([command, *args], cwd=cwd, env={**base_env, **(env or {})})

    base_env = env
    handle._test_root = tmp_path
    handle.exec_start = start
    return handle


def seed(sandbox, path="repo"):
    sandbox.git.init(path, initial_branch="trunk")
    (sandbox._test_root / f"{path}/initial.txt").write_text("initial\n")
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
    (sandbox._test_root / ("repo/" + unusual)).write_text("hello")
    status = sandbox.git.status("repo")
    assert status.file_status[0].name == unusual
    assert status.file_status[0].index == "?"
    sandbox.git.add("repo", [unusual])
    assert sandbox.git.status("repo").file_status[0].index == "A"
    sandbox.git.reset("repo", files=[unusual])
    assert sandbox.git.status("repo").file_status[0].index == "?"
    sandbox.git.add("repo", [unusual])
    sandbox.git.commit("repo", "add unusual path", "Test", "test@example.com")
    (sandbox._test_root / ("repo/" + unusual)).rename(
        sandbox._test_root / "repo/renamed.txt"
    )
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
    (sandbox._test_root / "origin/initial.txt").write_text("second")
    sandbox.git.add("origin", ["."])
    sandbox.git.commit("origin", "second", "Test", "test@example.com")
    url = (tmp_path / "origin").as_uri()
    sandbox.git.clone(url, "clone", depth=None)
    status = sandbox.git.status("clone")
    assert status.current_branch == "trunk" and not status.detached
    assert status.branch_published and status.upstream == "origin/trunk"
    assert sandbox.git.status("origin").upstream is None
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
    status = sandbox.git.status("detached")
    assert status.current_branch is None and status.detached
    assert (sandbox._test_root / "detached/initial.txt").read_bytes() == b"initial\n"


def test_remotes_push_pull_fetch_config(sandbox, tmp_path):
    seed(sandbox)
    sandbox.git.init("remote", bare=True, initial_branch="trunk")
    url = str(tmp_path / "remote")
    sandbox.git.remote_add("repo", "origin", url)
    assert sandbox.git.remote_get("repo", "origin").fetch_url == url
    assert sandbox.git.remotes("repo")[0].push_url == url
    sandbox.git.push("repo", set_upstream=True)
    sandbox.git.clone(url, "other")
    (sandbox._test_root / "repo/initial.txt").write_text("changed")
    sandbox.git.add("repo", ["."])
    sandbox.git.commit("repo", "change", "Test", "test@example.com")
    assert sandbox.git.status("repo").ahead == 1
    sandbox.git.push("repo")
    sandbox.git.fetch("other")
    assert sandbox.git.status("other").behind == 1
    sandbox.git.pull("other")
    assert (sandbox._test_root / "other/initial.txt").read_bytes() == b"changed"
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
    assert (
        password not in (sandbox._test_root / "clone/.git/config").read_bytes().decode()
    )
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
