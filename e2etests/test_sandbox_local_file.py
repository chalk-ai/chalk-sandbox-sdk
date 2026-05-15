"""End-to-end tests for the high-level Sandbox(image=Image(...)).run().exec() API.

Tests that Sandbox with Image.add_local_file (volume strategy) correctly
uploads local files, creates the sandbox, and makes the files readable
inside the sandbox.
"""

import os
import tempfile
import uuid
from pathlib import Path

import pytest

from chalkcompute import Image, Sandbox
from e2etests.conftest import needs_staging


@needs_staging
def test_sandbox_add_local_file() -> None:
    """Sandbox with add_local_file uploads a file and execs against it."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False,
    ) as f:
        f.write('print("hello from sandbox")')
        f.flush()
        local_path = f.name

    try:
        img = Image.debian_slim().add_local_file(
            local_path, "/app/app.py",
        )
        s = Sandbox(
            image=img,
            name=f"test-sandbox-local-file-{uuid.uuid4().hex[:8]}",
        )
        try:
            s.run()
            assert s.id is not None
            result = s.exec("python", "/app/app.py")
            assert result.exit_code == 0, (
                f"python exited with code {result.exit_code}: {result.stderr_text}"
            )
            assert result.stdout_text.strip() == "hello from sandbox"
        finally:
            s.terminate()
    finally:
        os.unlink(local_path)


@needs_staging
def test_sandbox_add_local_dir() -> None:
    """Sandbox with add_local_dir uploads a directory and execs against it."""
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "greeting.txt").write_text("hello")
        sub = Path(d) / "sub"
        sub.mkdir()
        (sub / "nested.txt").write_text("world")

        img = Image.debian_slim().add_local_dir(d, "/app/mydir")
        s = Sandbox(
            image=img,
            name=f"test-sandbox-local-dir-{uuid.uuid4().hex[:8]}",
        )
        try:
            s.run()

            r1 = s.exec("cat", "/app/mydir/greeting.txt")
            assert r1.exit_code == 0, f"cat greeting.txt failed: {r1.stderr_text}"
            assert r1.stdout_text.strip() == "hello"

            r2 = s.exec("cat", "/app/mydir/sub/nested.txt")
            assert r2.exit_code == 0, f"cat sub/nested.txt failed: {r2.stderr_text}"
            assert r2.stdout_text.strip() == "world"
        finally:
            s.terminate()


@needs_staging
def test_sandbox_exec_and_terminate() -> None:
    """Basic Sandbox lifecycle: run, exec, terminate."""
    s = Sandbox(
        image=Image.debian_slim(),
        name=f"test-sandbox-lifecycle-{uuid.uuid4().hex[:8]}",
    )
    try:
        s.run()
        assert s.id is not None
        assert s.info is not None
        assert s.info.status == "ready"

        result = s.exec("echo", "sandbox works")
        assert result.exit_code == 0
        assert result.stdout_text.strip() == "sandbox works"
    finally:
        s.terminate()
