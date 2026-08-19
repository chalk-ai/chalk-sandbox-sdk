"""Integration tests for Image building against the staging sandbox service.

These tests require CHALK_CLIENT_ID/CHALK_API_SERVER env vars.
Unit tests have been moved to tests/test_image.py.
"""

import json
import time

import pytest

from chalkcompute import Image
from e2etests.conftest import _connect_rpc, needs_staging

_SANDBOX_SVC = "chalk.sandbox.v1.SandboxService"
_CUSTOM_IMAGE_SVC = "chalk.sandbox.v1.CustomImageService"

POLL_INTERVAL = 5
POLL_TIMEOUT = 600


def _poll_until_ready(sandbox_id: str, headers: dict[str, str]) -> dict:
    """Poll GetSandbox until the sandbox reaches 'ready' or 'error' state."""
    deadline = time.monotonic() + POLL_TIMEOUT
    while True:
        resp = _connect_rpc(
            _SANDBOX_SVC, "GetSandbox", {"sandboxId": sandbox_id}, headers
        )
        assert resp.status_code == 200, f"GetSandbox failed: {resp.text}"
        data = resp.json()
        state = data.get("sandbox", {}).get("state", "")
        build_id = data.get("sandbox", {}).get("buildId", "")
        print(f"  poll: state={state} build_id={build_id}")
        if state == "ready":
            return data
        if state in ("error", "terminated"):
            pytest.fail(f"Sandbox {sandbox_id} entered {state} state: {data}")
        if time.monotonic() >= deadline:
            break
        time.sleep(POLL_INTERVAL)
    pytest.fail(f"Timed out after {POLL_TIMEOUT}s waiting for sandbox {sandbox_id}")


@needs_staging
def test_list_sandboxes(auth_headers: dict[str, str]) -> None:
    """Verify we can authenticate and call ListSandboxes."""
    resp = _connect_rpc(_SANDBOX_SVC, "ListSandboxes", {}, auth_headers)
    assert resp.status_code == 200, f"ListSandboxes failed: {resp.text}"
    data = resp.json()
    assert isinstance(data, dict)
    print(f"ListSandboxes response: {data}")


@needs_staging
@pytest.mark.skip(
    reason="Sandbox pod connectivity is flaky in staging — sandbox enters error/timeout after image build"
)
def test_create_sandbox_with_debian_image(auth_headers: dict[str, str]) -> None:
    """Create a sandbox with a debian_slim ImageSpec using async polling."""
    img = (
        Image.debian_slim()
        .pip_install(["requests"])
        .run_commands("echo 'image built successfully'")
        .workdir("/home/user")
        .env({"TEST_VAR": "hello"})
    )

    spec_dict = json.loads(img.to_proto().to_json())
    body = {
        "imageSpec": spec_dict,
        "name": "test-debian-slim",
    }

    resp = _connect_rpc(_SANDBOX_SVC, "CreateSandbox", body, auth_headers, timeout=30)
    assert resp.status_code == 200, f"CreateSandbox failed: {resp.text}"

    data = resp.json()
    print(f"CreateSandbox response: {data}")

    assert "sandbox" in data
    sandbox_id = data["sandbox"]["id"]
    state = data["sandbox"]["state"]
    build_id = data["sandbox"].get("buildId")
    print(f"Created sandbox: {sandbox_id} state={state} build_id={build_id}")

    if state in ("building", "connecting"):
        print(f"Polling until ready (build_id={build_id})...")
        ready_data = _poll_until_ready(sandbox_id, auth_headers)
        assert ready_data["sandbox"]["state"] == "ready"
        print(f"Sandbox {sandbox_id} is ready")
    else:
        assert state == "ready", f"Unexpected state: {state}"

    _connect_rpc(
        _SANDBOX_SVC, "TerminateSandbox", {"sandboxId": sandbox_id}, auth_headers
    )


@needs_staging
@pytest.mark.skip(
    reason="Sandbox pod connectivity is flaky in staging — 504 gateway timeout"
)
def test_create_sandbox_with_plain_image(auth_headers: dict[str, str]) -> None:
    """Create a sandbox with a plain image string and poll until ready."""
    body = {
        "image": "python:3.14-slim-trixie",
        "name": "test-plain-image",
    }

    resp = _connect_rpc(_SANDBOX_SVC, "CreateSandbox", body, auth_headers, timeout=30)
    assert resp.status_code == 200, f"CreateSandbox failed: {resp.text}"

    data = resp.json()
    print(f"CreateSandbox (plain) response: {data}")

    assert "sandbox" in data
    sandbox_id = data["sandbox"]["id"]
    state = data["sandbox"]["state"]

    if state in ("building", "connecting"):
        print("Polling until ready...")
        ready_data = _poll_until_ready(sandbox_id, auth_headers)
        assert ready_data["sandbox"]["state"] == "ready"
    else:
        assert state == "ready", f"Unexpected state: {state}"

    print(f"Sandbox {sandbox_id} is ready")
    _connect_rpc(
        _SANDBOX_SVC, "TerminateSandbox", {"sandboxId": sandbox_id}, auth_headers
    )


@needs_staging
def test_build_custom_image(auth_headers: dict[str, str]) -> None:
    """Build a custom image via CustomImageService.BuildCustomImage and query it via GetCustomImage."""
    img = (
        Image.debian_slim()
        .pip_install(["requests"])
        .run_commands("echo 'custom image built'")
    )

    spec_dict = json.loads(img.to_proto().to_json())
    body = {"imageSpec": spec_dict}

    resp = _connect_rpc(
        _CUSTOM_IMAGE_SVC, "BuildCustomImage", body, auth_headers, timeout=600
    )
    assert resp.status_code == 200, f"BuildCustomImage failed: {resp.text}"

    data = resp.json()
    print(f"BuildCustomImage response: {data}")
    assert "image" in data
    assert "buildId" in data

    build_id = data["buildId"]

    # Query the build by ID
    get_resp = _connect_rpc(
        _CUSTOM_IMAGE_SVC, "GetCustomImage", {"buildId": build_id}, auth_headers
    )
    assert get_resp.status_code == 200, f"GetCustomImage failed: {get_resp.text}"

    get_data = get_resp.json()
    print(f"GetCustomImage response: {get_data}")
    assert get_data["buildId"] == build_id
    assert get_data["status"] == "succeeded"
    assert get_data["image"] == data["image"]
