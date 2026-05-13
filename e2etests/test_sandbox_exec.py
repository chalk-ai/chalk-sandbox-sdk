"""End-to-end tests for sandbox exec and PTY interaction.

These tests:
  1. Create a sandbox with the public SandboxClient API
  2. Exec 'ls' and verify output
  3. Start an interactive shell, send a command via stdin, read output
  4. Clean up the sandbox
"""

import os
import uuid
from collections.abc import Iterator

import pytest
from dotenv import load_dotenv

from chalkcompute import Sandbox, SandboxClient

load_dotenv()

CHALK_CLIENT_ID = os.environ.get("CHALK_CLIENT_ID", "")
CHALK_API_SERVER = os.environ.get("CHALK_API_SERVER", "")

POLL_INTERVAL = 0.5
POLL_TIMEOUT = 300

needs_staging = pytest.mark.skipif(
    not CHALK_CLIENT_ID or not CHALK_API_SERVER,
    reason="CHALK_CLIENT_ID/CHALK_API_SERVER not set",
)


@pytest.fixture(scope="module")
def sandbox_client() -> Iterator[SandboxClient]:
    with SandboxClient.from_env() as client:
        yield client


@pytest.fixture(scope="module")
def sandbox(sandbox_client: SandboxClient) -> Iterator[Sandbox]:
    """Create a sandbox and clean it up after the exec tests."""
    created = sandbox_client.create(
        "debian:bookworm-slim",
        name=f"test-exec-e2e-{uuid.uuid4().hex[:8]}",
        poll_interval=POLL_INTERVAL,
        timeout=POLL_TIMEOUT,
    )
    try:
        assert created.info.status == "ready"
        yield created
    finally:
        created.terminate()


@needs_staging
def test_exec_ls(sandbox: Sandbox) -> None:
    """Exec 'ls /' in the sandbox and verify we get directory listing output."""
    stdout_parts: list[str] = []
    exit_code: int | None = None

    for event in sandbox.exec_stream("ls", "/"):
        if event.stdout:
            stdout_parts.append(event.stdout)
        if event.is_exited:
            exit_code = event.exit_code
        if event.is_error:
            pytest.fail(f"Exec error: {event.error_code}: {event.error_message}")

    stdout = "".join(stdout_parts)
    print(f"ls / output:\n{stdout}")

    assert exit_code == 0, f"ls exited with code {exit_code}"
    # A Linux container root should have standard directories
    for expected_dir in ("bin", "etc", "usr", "tmp"):
        assert expected_dir in stdout, f"Expected '{expected_dir}' in ls output: {stdout}"


@needs_staging
def test_pty_interaction(sandbox: Sandbox) -> None:
    """Start an interactive shell, send a command via stdin, and verify output."""
    # Start bash interactively
    process = sandbox.exec_start("bash")

    # Send a command and then exit
    process.write_stdin("echo CHALK_PTY_TEST_OK\n")
    process.write_stdin("exit\n")

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    exit_code: int | None = None

    for event in process.output():
        if event.stdout:
            stdout_parts.append(event.stdout)
            print(f"  stdout: {event.stdout!r}")
        if event.stderr:
            stderr_parts.append(event.stderr)
            print(f"  stderr: {event.stderr!r}")
        if event.is_exited:
            exit_code = event.exit_code
            break
        if event.is_error:
            pytest.fail(f"Exec error: {event.error_code}: {event.error_message}")

    full_stdout = "".join(stdout_parts)
    print(f"Full stdout: {full_stdout!r}")

    assert "CHALK_PTY_TEST_OK" in full_stdout, (
        f"Expected 'CHALK_PTY_TEST_OK' in stdout, got: {full_stdout!r}"
    )
    assert exit_code == 0, f"bash exited with code {exit_code}"
