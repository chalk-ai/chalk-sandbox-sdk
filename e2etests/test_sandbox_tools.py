"""Git in the default sandbox image and opt-in tmux image support."""

from __future__ import annotations

from chalkcompute import Sandbox, SandboxClient
from e2etests._ci_resources import build_scoped_name
from e2etests.conftest import needs_staging


@needs_staging
def test_default_sandbox_git():
    with SandboxClient.from_env() as client:
        sandbox = Sandbox(
            name=build_scoped_name("sandbox-tools"),
            lifetime="600s",
            sandbox_client=client,
        )
        try:
            sandbox.run(timeout=300)
            assert sandbox.exec("pwd").stdout_text.strip() == "/workspace"
            sandbox.git.init("repo", initial_branch="trunk")
            sandbox.exec("sh", "-c", "printf 'hello sandbox\\n' > repo/README.md")
            sandbox.git.add("repo", ["."])
            commit = sandbox.git.commit(
                "repo", "initial", "SDK Test", "sdk@example.com"
            )
            assert len(commit) == 40
            sandbox.git.clone("file:///workspace/repo", "clone")
            status = sandbox.git.status("clone")
            assert status.current_branch == "trunk"
            assert status.file_status == []
            assert (
                sandbox.exec("cat", "clone/README.md").stdout_text == "hello sandbox\n"
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
            assert sandbox.exec("cat", "/workspace/tmux-output").stdout == b"tmux-ok"
            capture = sandbox.exec("tmux", "capture-pane", "-p", "-t", "work")
            assert capture.exit_code == 0, capture.stderr_text
            assert "tmux-ok" in capture.stdout_text
            assert sandbox.exec("tmux", "kill-session", "-t", "work").exit_code == 0
            assert sandbox.exec("tmux", "has-session", "-t", "work").exit_code != 0
        finally:
            if sandbox.id is not None:
                sandbox.terminate()


@needs_staging
def test_sandbox_agent_tools_image():
    import json

    from chalkcompute import GitConfig, Image

    with SandboxClient.from_env() as client:
        sandbox = Sandbox(
            image=Image.sandbox()
            .with_claude_code("2.1.270", trusted_dirs=["/workspace/app"])
            .with_gh()
            .with_tmux(),
            name=build_scoped_name("sandbox-agent-tools"),
            env={**GitConfig("Sandbox Bot", "bot@example.com")},
            lifetime="600s",
            sandbox_client=client,
        )
        try:
            sandbox.run(timeout=600)
            claude = sandbox.exec("claude", "--version")
            assert claude.exit_code == 0, claude.stderr_text
            assert claude.stdout_text.startswith("2.1.270")
            assert sandbox.exec("which", "node").exit_code != 0
            gh = sandbox.exec("gh", "--version")
            assert gh.exit_code == 0, gh.stderr_text
            assert gh.stdout_text.startswith("gh version ")
            tmux = sandbox.exec("tmux", "-V")
            assert tmux.exit_code == 0, tmux.stderr_text
            config = sandbox.exec("cat", "/root/.claude.json")
            assert json.loads(config.stdout_text) == {
                "hasCompletedOnboarding": True,
                "projects": {"/workspace/app": {"hasTrustDialogAccepted": True}},
            }
            ident = sandbox.exec("git", "var", "GIT_AUTHOR_IDENT")
            assert ident.stdout_text.startswith("Sandbox Bot <bot@example.com> ")
        finally:
            if sandbox.id is not None:
                sandbox.terminate()
