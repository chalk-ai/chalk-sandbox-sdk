"""End-to-end tests for the managed Sandbox abstraction.

Tests the high-level Sandbox(image=...).run().exec().terminate() flow.

Tests using the pre-built sandbox image run against staging.
Tests requiring custom Image builds are skipped pending server-side support.
"""

import os
import uuid

import pytest
from dotenv import load_dotenv

from chalkcompute import Image, Sandbox

load_dotenv()

CHALK_CLIENT_ID = os.environ.get("CHALK_CLIENT_ID", "")
CHALK_API_SERVER = os.environ.get("CHALK_API_SERVER", "")

SANDBOX_IMAGE = "us-central1-docker.pkg.dev/chalk-develop/base-images/chalk-sandbox-entrypoint:latest"

needs_staging = pytest.mark.skipif(
    not CHALK_CLIENT_ID or not CHALK_API_SERVER,
    reason="CHALK_CLIENT_ID/CHALK_API_SERVER not set",
)

needs_custom_image = pytest.mark.skip(
    reason="Requires custom image build support (Image spec); not yet available",
)


# ---------------------------------------------------------------------------
# Pre-built image tests (SANDBOX_IMAGE)
# ---------------------------------------------------------------------------


@needs_staging
def test_sandbox_boot_and_ls() -> None:
    """Boot a sandbox and run 'ls /'."""
    s = Sandbox(
        image=SANDBOX_IMAGE,
        name=f"test-sandbox-ls-{uuid.uuid4().hex[:8]}",
    )
    try:
        s.run()
        result = s.exec("ls", "/")
        assert result.exit_code == 0, f"ls exited {result.exit_code}: {result.stderr_text}"
        for expected in ("bin", "etc", "usr", "tmp"):
            assert expected in result.stdout_text, (
                f"Expected '{expected}' in ls output: {result.stdout_text}"
            )
    finally:
        s.terminate()


@needs_staging
def test_sandbox_exec_echo() -> None:
    """Boot a sandbox and verify echo works."""
    s = Sandbox(
        image=SANDBOX_IMAGE,
        name=f"test-sandbox-echo-{uuid.uuid4().hex[:8]}",
    )
    try:
        s.run()
        result = s.exec("echo", "hello sandbox")
        assert result.exit_code == 0
        assert result.stdout_text.strip() == "hello sandbox"
    finally:
        s.terminate()


@needs_staging
def test_sandbox_exec_multiline() -> None:
    """Verify multi-line stdout is captured correctly."""
    s = Sandbox(
        image=SANDBOX_IMAGE,
        name=f"test-sandbox-multi-{uuid.uuid4().hex[:8]}",
    )
    try:
        s.run()
        result = s.exec("sh", "-c", "echo line1; echo line2; echo line3")
        assert result.exit_code == 0
        lines = result.stdout_text.strip().splitlines()
        assert lines == ["line1", "line2", "line3"]
    finally:
        s.terminate()


@needs_staging
def test_sandbox_exec_exit_code() -> None:
    """Verify non-zero exit codes are captured."""
    s = Sandbox(
        image=SANDBOX_IMAGE,
        name=f"test-sandbox-exit-{uuid.uuid4().hex[:8]}",
    )
    try:
        s.run()
        result = s.exec("sh", "-c", "exit 42")
        assert result.exit_code == 42
    finally:
        s.terminate()


@needs_staging
def test_sandbox_from_id() -> None:
    """Create a sandbox, then attach to it via from_id."""
    s = Sandbox(
        image=SANDBOX_IMAGE,
        name=f"test-sandbox-fromid-{uuid.uuid4().hex[:8]}",
    )
    try:
        s.run()
        sandbox_id = s.id
        assert sandbox_id is not None

        s2 = Sandbox.from_id(sandbox_id)
        result = s2.exec("echo", "attached")
        assert result.exit_code == 0
        assert result.stdout_text.strip() == "attached"
    finally:
        s.terminate()


@needs_staging
def test_sandbox_from_name() -> None:
    """Create a sandbox, then attach to it via from_name."""
    sandbox_name = f"test-sandbox-fromname-{uuid.uuid4().hex[:8]}"
    s = Sandbox(
        image=SANDBOX_IMAGE,
        name=sandbox_name,
    )
    try:
        s.run()

        s2 = Sandbox.from_name(sandbox_name)
        result = s2.exec("echo", "found by name")
        assert result.exit_code == 0
        assert result.stdout_text.strip() == "found by name"
    finally:
        s.terminate()


# ---------------------------------------------------------------------------
# Custom Image tests (skipped — require image build support)
# ---------------------------------------------------------------------------


@needs_staging
@needs_custom_image
def test_sandbox_custom_image_boot() -> None:
    """Boot a sandbox with a custom Image spec."""
    s = Sandbox(
        image=Image.debian_slim(),
        name=f"test-sandbox-custom-{uuid.uuid4().hex[:8]}",
    )
    try:
        s.run()
        result = s.exec("python3", "--version")
        assert result.exit_code == 0
    finally:
        s.terminate()


@needs_staging
@needs_custom_image
def test_sandbox_env_vars() -> None:
    """Verify environment variables are passed to the sandbox."""
    s = Sandbox(
        image=Image.debian_slim(),
        name=f"test-sandbox-env-{uuid.uuid4().hex[:8]}",
        env={"MY_TEST_VAR": "chalk_sandbox_test_value"},
    )
    try:
        s.run()
        result = s.exec("sh", "-c", "echo $MY_TEST_VAR")
        assert result.exit_code == 0
        assert result.stdout_text.strip() == "chalk_sandbox_test_value"
    finally:
        s.terminate()


@needs_staging
@needs_custom_image
def test_sandbox_local_file_volume() -> None:
    """Inject a local Python script via volume and execute it."""
    import tempfile

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False,
    ) as f:
        f.write('print("hello from managed sandbox")\n')
        f.flush()
        local_path = f.name

    try:
        img = Image.debian_slim().add_local_file(
            local_path, "/app/app.py", strategy="volume",
        )
        s = Sandbox(
            image=img,
            name=f"test-sandbox-vol-{uuid.uuid4().hex[:8]}",
        )
        try:
            s.run()
            result = s.exec("python3", "/app/app.py")
            assert result.exit_code == 0, (
                f"python3 exited {result.exit_code}: {result.stderr_text}"
            )
            assert result.stdout_text.strip() == "hello from managed sandbox"
        finally:
            s.terminate()
    finally:
        os.unlink(local_path)
