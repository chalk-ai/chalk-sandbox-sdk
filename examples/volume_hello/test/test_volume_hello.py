"""
Volume hello-world test.

Proves that a file uploaded to a named volume is readable inside a container
that mounts it. ChalkFS may take a few seconds to sync after the container
starts, so we retry the read with a short backoff.

    uv run python -m pytest examples/volume_hello/test/test_volume_hello.py -v -s
"""

import base64
import os
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest
from dotenv import load_dotenv

from chalkcompute import Container

load_dotenv()

CHALK_CLIENT_ID = os.environ.get("CHALK_CLIENT_ID")
CHALK_API_SERVER = os.environ.get("CHALK_API_SERVER")

needs_staging = pytest.mark.skipif(
    not CHALK_CLIENT_ID or not CHALK_API_SERVER,
    reason="CHALK_CLIENT_ID/CHALK_API_SERVER not set",
)

MOUNT_PATH = "/mnt/data"
FILE_NAME = "hello.txt"
FILE_CONTENT = "Hello from a Chalk volume!"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_token() -> str:
    resp = httpx.post(
        f"{os.environ['CHALK_API_SERVER']}/v1/oauth/token",
        json={
            "client_id": os.environ["CHALK_CLIENT_ID"],
            "client_secret": os.environ["CHALK_CLIENT_SECRET"],
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _volume_rpc(method: str, body: dict, *, token: str) -> httpx.Response:
    return httpx.post(
        f"{os.environ['CHALK_API_SERVER']}/chalk.volume.v1.VolumeService/{method}",
        json=body,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Chalk-Env-Id": os.environ.get("CHALK_ENVIRONMENT_ID", ""),
            "Content-Type": "application/json",
        },
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def token() -> str:
    return _get_token()


@pytest.fixture
def hello_vol(token: str) -> Iterator[str]:
    """Create a volume with hello.txt, yield its name, then delete it."""
    vol_name = f"test-hello-vol-{uuid.uuid4().hex[:8]}"

    resp = _volume_rpc("CreateVolume", {"name": vol_name}, token=token)
    resp.raise_for_status()
    resp = _volume_rpc(
        "PutFile",
        {
            "volumeName": vol_name,
            "path": FILE_NAME,
            "data": base64.b64encode(FILE_CONTENT.encode()).decode(),
        },
        token=token,
    )
    resp.raise_for_status()

    yield vol_name

    _volume_rpc("DeleteVolume", {"name": vol_name}, token=token)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@needs_staging
def test_file_readable_in_container(hello_vol: str) -> None:
    """A file uploaded to a volume is visible at the mount path inside a container."""
    env: dict[str, str] = {}
    if v := os.environ.get("CHALK_CLIENT_ID"):
        env["CHALK_CLIENT_ID"] = v
    if v := os.environ.get("CHALK_CLIENT_SECRET"):
        env["CHALK_CLIENT_SECRET"] = v

    container = Container(
        image="python:3.12-slim-bookworm",
        volumes=[(hello_vol, MOUNT_PATH)],
        env=env,
    )
    try:
        container.run()

        # ChalkFS may take a few seconds to sync after the container starts.
        result = None
        for _ in range(12):
            result = container.exec("cat", f"{MOUNT_PATH}/{FILE_NAME}")
            if result.exit_code == 0:
                break
            time.sleep(5)

        assert result is not None
        assert result.exit_code == 0, f"cat failed: {result.stderr_text}"
        assert result.stdout_text.strip() == FILE_CONTENT.strip()
    finally:
        container.stop()
