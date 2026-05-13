"""End-to-end tests for sandbox exec and PTY interaction against ftqa.

These tests:
  1. Create a sandbox with a pre-built image via Connect protocol
  2. Exec 'ls' and verify output via gRPC bidirectional streaming
  3. Start an interactive shell (PTY-like), send a command via stdin, read output
  4. Clean up the sandbox
"""

import os
import time

import grpc
import pytest
import requests
from dotenv import load_dotenv

from chalkcompute import SandboxClient

load_dotenv()

CHALK_CLIENT_ID = os.environ.get("CHALK_CLIENT_ID", "")
CHALK_CLIENT_SECRET = os.environ.get("CHALK_CLIENT_SECRET", "")
CHALK_ENVIRONMENT_ID = os.environ.get("CHALK_ENVIRONMENT_ID", "")
CHALK_API_SERVER = os.environ.get("CHALK_API_SERVER", "")

SANDBOX_IMAGE = (
    "us-central1-docker.pkg.dev/chalk-develop/base-images/chalk-sandbox-entrypoint"
)

SANDBOX_SERVICE_BASE = f"{CHALK_API_SERVER}/chalk.sandbox.v1.SandboxService"

POLL_INTERVAL = 5
POLL_TIMEOUT = 300

needs_staging = pytest.mark.skipif(
    not CHALK_CLIENT_ID or not CHALK_API_SERVER,
    reason="CHALK_CLIENT_ID/CHALK_API_SERVER not set",
)


def _get_access_token() -> str:
    """Exchange client credentials for a bearer token."""
    resp = requests.post(
        f"{CHALK_API_SERVER}/v1/oauth/token",
        json={
            "client_id": CHALK_CLIENT_ID,
            "client_secret": CHALK_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _sandbox_rpc(
    method: str, body: dict, headers: dict[str, str], timeout: float = 30
) -> requests.Response:
    """Make a Connect-protocol unary RPC call to SandboxService."""
    return requests.post(
        f"{SANDBOX_SERVICE_BASE}/{method}",
        json=body,
        headers=headers,
        timeout=timeout,
    )


def _poll_until_ready(sandbox_id: str, headers: dict[str, str]) -> dict:
    """Poll GetSandbox until the sandbox reaches 'ready' or 'error' state."""
    deadline = time.monotonic() + POLL_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        resp = _sandbox_rpc("GetSandbox", {"sandboxId": sandbox_id}, headers)
        assert resp.status_code == 200, f"GetSandbox failed: {resp.text}"
        data = resp.json()
        state = data.get("sandbox", {}).get("state", "")
        print(f"  poll: state={state}")
        if state == "ready":
            return data
        if state in ("error", "terminated"):
            pytest.fail(f"Sandbox {sandbox_id} entered {state} state: {data}")
    pytest.fail(f"Timed out after {POLL_TIMEOUT}s waiting for sandbox {sandbox_id}")


@pytest.fixture(scope="module")
def access_token() -> str:
    return _get_access_token()


@pytest.fixture(scope="module")
def connect_headers(access_token: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "X-Chalk-Env-Id": CHALK_ENVIRONMENT_ID,
    }


@pytest.fixture(scope="module")
def grpc_client(access_token: str) -> SandboxClient:
    """Create a gRPC SandboxClient pointed at the staging API server."""
    # Extract host from API server URL
    host = CHALK_API_SERVER.replace("https://", "").replace("http://", "")
    target = f"{host}:443"

    credentials = grpc.ssl_channel_credentials()
    metadata = [
        ("authorization", f"Bearer {access_token}"),
        ("x-chalk-env-id", CHALK_ENVIRONMENT_ID),
    ]
    client = SandboxClient(target, credentials=credentials, metadata=metadata)
    yield client
    client.close()


@pytest.fixture(scope="module")
def sandbox_id(connect_headers: dict[str, str]) -> str:
    """Create a sandbox with the pre-built image and wait until ready. Clean up after tests."""
    body = {
        "image": SANDBOX_IMAGE,
        "name": "test-exec-e2e",
    }
    resp = _sandbox_rpc("CreateSandbox", body, connect_headers, timeout=60)
    assert resp.status_code == 200, f"CreateSandbox failed: {resp.text}"

    data = resp.json()
    print(f"CreateSandbox response: {data}")
    assert "sandbox" in data

    sid = data["sandbox"]["id"]
    state = data["sandbox"]["state"]
    print(f"Created sandbox: {sid} state={state}")

    if state != "ready":
        print("Polling until ready...")
        ready_data = _poll_until_ready(sid, connect_headers)
        assert ready_data["sandbox"]["state"] == "ready"

    print(f"Sandbox {sid} is ready")
    yield sid

    # Cleanup
    _sandbox_rpc("TerminateSandbox", {"sandboxId": sid}, connect_headers)
    print(f"Terminated sandbox {sid}")


@needs_staging
@pytest.mark.xfail(reason="HTTP read timeout due to tcp interceptor issues")
def test_exec_ls(grpc_client: SandboxClient, sandbox_id: str) -> None:
    """Exec 'ls /' in the sandbox and verify we get directory listing output."""
    sandbox = grpc_client.get(id=sandbox_id)

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
        assert expected_dir in stdout, (
            f"Expected '{expected_dir}' in ls output: {stdout}"
        )


@needs_staging
@pytest.mark.xfail(reason="HTTP read timeout due to tcp interceptor issues")
def test_pty_interaction(grpc_client: SandboxClient, sandbox_id: str) -> None:
    """Start an interactive shell, send a command via stdin, and verify output."""
    sandbox = grpc_client.get(id=sandbox_id)

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
