"""Integration tests for image.py — attempts to build a debian slim image via the staging sandbox service.

Uses the Connect protocol (HTTP+JSON) since the go-api-server is a Connect server.
"""

import os
import time

import httpx
import pytest
from dotenv import load_dotenv
from google.protobuf.json_format import MessageToDict

from image import Image

load_dotenv()

CHALK_CLIENT_ID = os.environ["CHALK_CLIENT_ID"]
CHALK_CLIENT_SECRET = os.environ["CHALK_CLIENT_SECRET"]
CHALK_ENVIRONMENT_ID = os.environ["CHALK_ENVIRONMENT_ID"]
CHALK_API_SERVER = os.environ["CHALK_API_SERVER"]

SERVICE_BASE = f"{CHALK_API_SERVER}/chalk.sandbox.v1.SandboxService"

POLL_INTERVAL = 5
POLL_TIMEOUT = 600


def _get_access_token() -> str:
    """Exchange client credentials for a bearer token via the Chalk REST API."""
    resp = httpx.post(
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


@pytest.fixture(scope="module")
def auth_headers() -> dict[str, str]:
    token = _get_access_token()
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "X-Chalk-Env-Id": CHALK_ENVIRONMENT_ID,
    }


def _rpc(method: str, body: dict, headers: dict[str, str], timeout: float = 30) -> httpx.Response:
    """Make a Connect-protocol unary RPC call."""
    return httpx.post(
        f"{SERVICE_BASE}/{method}",
        json=body,
        headers=headers,
        timeout=timeout,
    )


def _poll_until_ready(sandbox_id: str, headers: dict[str, str]) -> dict:
    """Poll GetSandbox until the sandbox reaches 'ready' or 'error' state."""
    deadline = time.monotonic() + POLL_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        resp = _rpc("GetSandbox", {"sandboxId": sandbox_id}, headers)
        assert resp.status_code == 200, f"GetSandbox failed: {resp.text}"
        data = resp.json()
        state = data.get("sandbox", {}).get("state", "")
        build_id = data.get("sandbox", {}).get("buildId", "")
        print(f"  poll: state={state} build_id={build_id}")
        if state == "ready":
            return data
        if state in ("error", "terminated"):
            pytest.fail(f"Sandbox {sandbox_id} entered {state} state: {data}")
    pytest.fail(f"Timed out after {POLL_TIMEOUT}s waiting for sandbox {sandbox_id}")


def test_image_proto_serialization() -> None:
    """Verify Image.debian_slim() produces a valid protobuf ImageSpec."""
    img = (
        Image.debian_slim("3.12")
        .pip_install(["requests"])
        .run_commands("echo 'hello'")
        .workdir("/home/user")
        .env({"TEST_VAR": "hello"})
    )
    spec = img.to_proto()
    assert spec.base_image == "python:3.12-slim-bookworm"
    assert len(spec.steps) == 2
    assert spec.steps[0].pip_install.packages == ["requests"]
    assert spec.steps[1].run_commands.commands == ["echo 'hello'"]
    assert spec.workdir == "/home/user"
    assert spec.env["TEST_VAR"] == "hello"

    # Verify JSON serialization for Connect protocol
    spec_dict = MessageToDict(spec, preserving_proto_field_name=True)
    assert spec_dict["base_image"] == "python:3.12-slim-bookworm"
    assert "steps" in spec_dict
    assert "env" in spec_dict


def test_list_sandboxes(auth_headers: dict[str, str]) -> None:
    """Verify we can authenticate and call ListSandboxes."""
    resp = _rpc("ListSandboxes", {}, auth_headers)
    assert resp.status_code == 200, f"ListSandboxes failed: {resp.text}"
    data = resp.json()
    # Response is either {} (empty) or has "sandboxes" key
    assert isinstance(data, dict)
    print(f"ListSandboxes response: {data}")


def test_create_sandbox_with_debian_image(auth_headers: dict[str, str]) -> None:
    """Create a sandbox with a debian_slim ImageSpec using async polling.

    The server returns immediately with state="building", and we poll
    GetSandbox until the sandbox reaches "ready".
    """
    img = (
        Image.debian_slim("3.12")
        .pip_install(["requests"])
        .run_commands("echo 'image built successfully'")
        .workdir("/home/user")
        .env({"TEST_VAR": "hello"})
    )

    spec_dict = MessageToDict(img.to_proto())
    body = {
        "imageSpec": spec_dict,
        "name": "test-debian-slim",
    }

    resp = _rpc("CreateSandbox", body, auth_headers, timeout=30)
    assert resp.status_code == 200, f"CreateSandbox failed: {resp.text}"

    data = resp.json()
    print(f"CreateSandbox response: {data}")

    assert "sandbox" in data
    sandbox_id = data["sandbox"]["id"]
    state = data["sandbox"]["state"]
    build_id = data["sandbox"].get("buildId")
    print(f"Created sandbox: {sandbox_id} state={state} build_id={build_id}")

    # For image_spec builds, the server returns immediately with state="building"
    if state in ("building", "connecting"):
        print(f"Polling until ready (build_id={build_id})...")
        ready_data = _poll_until_ready(sandbox_id, auth_headers)
        assert ready_data["sandbox"]["state"] == "ready"
        print(f"Sandbox {sandbox_id} is ready")
    else:
        assert state == "ready", f"Unexpected state: {state}"

    # Clean up
    _rpc("TerminateSandbox", {"sandboxId": sandbox_id}, auth_headers)


def test_create_sandbox_with_plain_image(auth_headers: dict[str, str]) -> None:
    """Create a sandbox with a plain image string and poll until ready."""
    body = {
        "image": "python:3.12-slim-bookworm",
        "name": "test-plain-image",
    }

    resp = _rpc("CreateSandbox", body, auth_headers, timeout=30)
    assert resp.status_code == 200, f"CreateSandbox failed: {resp.text}"

    data = resp.json()
    print(f"CreateSandbox (plain) response: {data}")

    assert "sandbox" in data
    sandbox_id = data["sandbox"]["id"]
    state = data["sandbox"]["state"]

    if state in ("building", "connecting"):
        print(f"Polling until ready...")
        ready_data = _poll_until_ready(sandbox_id, auth_headers)
        assert ready_data["sandbox"]["state"] == "ready"
    else:
        assert state == "ready", f"Unexpected state: {state}"

    print(f"Sandbox {sandbox_id} is ready")
    _rpc("TerminateSandbox", {"sandboxId": sandbox_id}, auth_headers)
