"""Git and binary file operations using the default sandbox image."""

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
