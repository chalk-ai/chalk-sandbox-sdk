"""End-to-end tests for the Volume service against fraud-template-staging.

These tests exercise the VolumeService Connect-protocol endpoints:
  - CreateVolume
  - GetVolume
  - ListVolumes
  - PutFile
  - GetFile
  - ListFiles
  - RemoveFile
  - DeleteVolume
"""

import os
import uuid

import httpx
import pytest
from dotenv import load_dotenv

load_dotenv()

CHALK_CLIENT_ID = os.environ.get("CHALK_CLIENT_ID", "")
CHALK_CLIENT_SECRET = os.environ.get("CHALK_CLIENT_SECRET", "")
CHALK_ENVIRONMENT_ID = os.environ.get("CHALK_ENVIRONMENT_ID", "")
CHALK_API_SERVER = os.environ.get("CHALK_API_SERVER", "")

VOLUME_SERVICE_BASE = f"{CHALK_API_SERVER}/chalk.volume.v1.VolumeService"

needs_staging = pytest.mark.skipif(
    not CHALK_CLIENT_ID or not CHALK_API_SERVER,
    reason="CHALK_CLIENT_ID/CHALK_API_SERVER not set",
)


def _get_access_token() -> str:
    """Exchange client credentials for a bearer token."""
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


def _volume_rpc(
    method: str,
    body: dict,
    headers: dict[str, str],
    timeout: float = 30,
) -> httpx.Response:
    """Make a Connect-protocol unary RPC call to VolumeService."""
    return httpx.post(
        f"{VOLUME_SERVICE_BASE}/{method}",
        json=body,
        headers=headers,
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def test_volume_name() -> str:
    """Generate a unique volume name for this test run."""
    return f"test-volume-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def created_volume(
    auth_headers: dict[str, str],
    test_volume_name: str,
) -> str:
    """Create a volume for the test session and delete it afterwards."""
    resp = _volume_rpc("CreateVolume", {"name": test_volume_name}, auth_headers)
    assert resp.status_code == 200, f"CreateVolume failed: {resp.text}"
    data = resp.json()
    assert "volume" in data
    assert data["volume"]["name"] == test_volume_name
    yield test_volume_name

    # Cleanup
    _volume_rpc("DeleteVolume", {"name": test_volume_name}, auth_headers)


@needs_staging
def test_create_and_get_volume(
    auth_headers: dict[str, str],
    created_volume: str,
) -> None:
    """CreateVolume then GetVolume returns matching metadata."""
    resp = _volume_rpc("GetVolume", {"name": created_volume}, auth_headers)
    assert resp.status_code == 200, f"GetVolume failed: {resp.text}"
    data = resp.json()
    assert data["volume"]["name"] == created_volume
    assert "createdAt" in data["volume"]


@needs_staging
def test_list_volumes(
    auth_headers: dict[str, str],
    created_volume: str,
) -> None:
    """ListVolumes should include our test volume."""
    resp = _volume_rpc("ListVolumes", {}, auth_headers)
    assert resp.status_code == 200, f"ListVolumes failed: {resp.text}"
    data = resp.json()
    names = [v["name"] for v in data.get("volumes", [])]
    assert created_volume in names, f"Expected {created_volume} in {names}"


@needs_staging
def test_put_and_get_file(
    auth_headers: dict[str, str],
    created_volume: str,
) -> None:
    """PutFile then GetFile round-trips data correctly."""
    import base64

    file_path = "test-data/hello.txt"
    file_content = b"Hello from the volume e2e test!"
    encoded = base64.b64encode(file_content).decode("ascii")

    # Put the file
    put_resp = _volume_rpc(
        "PutFile",
        {
            "volumeName": created_volume,
            "path": file_path,
            "data": encoded,
        },
        auth_headers,
    )
    assert put_resp.status_code == 200, f"PutFile failed: {put_resp.text}"

    # Get the file back
    get_resp = _volume_rpc(
        "GetFile",
        {"volumeName": created_volume, "path": file_path},
        auth_headers,
    )
    assert get_resp.status_code == 200, f"GetFile failed: {get_resp.text}"
    returned_data = base64.b64decode(get_resp.json()["data"])
    assert returned_data == file_content


@needs_staging
def test_list_files(
    auth_headers: dict[str, str],
    created_volume: str,
) -> None:
    """ListFiles should show files we uploaded."""
    resp = _volume_rpc(
        "ListFiles",
        {"volumeName": created_volume, "prefix": "test-data/"},
        auth_headers,
    )
    assert resp.status_code == 200, f"ListFiles failed: {resp.text}"
    data = resp.json()
    paths = [f["path"] for f in data.get("files", [])]
    assert "test-data/hello.txt" in paths, f"Expected test-data/hello.txt in {paths}"


@needs_staging
def test_remove_file(
    auth_headers: dict[str, str],
    created_volume: str,
) -> None:
    """RemoveFile should delete a file, and it should no longer appear in ListFiles."""
    import base64

    # Put a file to remove
    file_path = "test-data/to-remove.txt"
    _volume_rpc(
        "PutFile",
        {
            "volumeName": created_volume,
            "path": file_path,
            "data": base64.b64encode(b"delete me").decode("ascii"),
        },
        auth_headers,
    )

    # Remove it
    rm_resp = _volume_rpc(
        "RemoveFile",
        {"volumeName": created_volume, "path": file_path},
        auth_headers,
    )
    assert rm_resp.status_code == 200, f"RemoveFile failed: {rm_resp.text}"

    # Verify it's gone
    list_resp = _volume_rpc(
        "ListFiles",
        {"volumeName": created_volume, "prefix": "test-data/"},
        auth_headers,
    )
    assert list_resp.status_code == 200
    paths = [f["path"] for f in list_resp.json().get("files", [])]
    assert file_path not in paths, f"File {file_path} should have been removed"


@needs_staging
def test_delete_volume(auth_headers: dict[str, str]) -> None:
    """Create and delete a standalone volume to verify DeleteVolume works."""
    vol_name = f"test-delete-{uuid.uuid4().hex[:8]}"

    create_resp = _volume_rpc("CreateVolume", {"name": vol_name}, auth_headers)
    assert create_resp.status_code == 200, f"CreateVolume failed: {create_resp.text}"

    delete_resp = _volume_rpc("DeleteVolume", {"name": vol_name}, auth_headers)
    assert delete_resp.status_code == 200, f"DeleteVolume failed: {delete_resp.text}"

    # GetVolume should fail
    get_resp = _volume_rpc("GetVolume", {"name": vol_name}, auth_headers)
    assert get_resp.status_code != 200 or "code" in get_resp.json(), (
        f"Expected volume {vol_name} to be deleted but GetVolume succeeded: {get_resp.text}"
    )
