"""End-to-end tests for the Container abstraction.

Tests that Container(image=Image(...).add_local_file(..., strategy="copy")) and
Container(image=Image(...).add_local_dir(..., strategy="copy")) correctly
inline local files into the image and make them readable inside the container.
"""

import os
import tempfile
import uuid
from pathlib import Path

import pytest
from dotenv import load_dotenv

from chalkcompute import Container, Image

load_dotenv()

CHALK_CLIENT_ID = os.environ.get("CHALK_CLIENT_ID", "")
CHALK_API_SERVER = os.environ.get("CHALK_API_SERVER", "")

needs_staging = pytest.mark.skipif(
    not CHALK_CLIENT_ID or not CHALK_API_SERVER,
    reason="CHALK_CLIENT_ID/CHALK_API_SERVER not set",
)


@needs_staging
def test_container_add_local_file() -> None:
    """Container with add_local_file(strategy='copy') bakes file into image."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("hello from local file")
        f.flush()
        local_path = f.name

    try:
        img = Image.debian_slim().add_local_file(local_path, "/app/data.txt", strategy="copy")
        c = Container(image=img, name=f"test-add-local-file-{uuid.uuid4().hex[:8]}")
        try:
            c.run()
            result = c.exec("cat", "/app/data.txt")
            assert result.exit_code == 0, (
                f"cat exited with code {result.exit_code}: {result.stderr_text}"
            )
            assert result.stdout_text.strip() == "hello from local file"
        finally:
            c.stop()
    finally:
        os.unlink(local_path)


@needs_staging
def test_container_add_local_dir() -> None:
    """Container with add_local_dir(strategy='copy') bakes files into image."""
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "greeting.txt").write_text("hello")
        sub = Path(d) / "sub"
        sub.mkdir()
        (sub / "nested.txt").write_text("world")

        img = Image.debian_slim().add_local_dir(d, "/app/mydir", strategy="copy")
        c = Container(image=img, name=f"test-add-local-dir-{uuid.uuid4().hex[:8]}")
        try:
            c.run()

            r1 = c.exec("cat", "/app/mydir/greeting.txt")
            assert r1.exit_code == 0, f"cat greeting.txt failed: {r1.stderr_text}"
            assert r1.stdout_text.strip() == "hello"

            r2 = c.exec("cat", "/app/mydir/sub/nested.txt")
            assert r2.exit_code == 0, f"cat sub/nested.txt failed: {r2.stderr_text}"
            assert r2.stdout_text.strip() == "world"
        finally:
            c.stop()
