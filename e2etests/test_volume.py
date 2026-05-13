"""End-to-end tests for the Volume SDK API.

These tests exercise:
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
from collections.abc import Iterator

import pytest
from dotenv import load_dotenv

from chalkcompute import ConnectClient, Volume, VolumeClient, VolumeNotFoundError

load_dotenv()

CHALK_CLIENT_ID = os.environ.get("CHALK_CLIENT_ID", "")
CHALK_API_SERVER = os.environ.get("CHALK_API_SERVER", "")

needs_staging = pytest.mark.skipif(
    not CHALK_CLIENT_ID or not CHALK_API_SERVER,
    reason="CHALK_CLIENT_ID/CHALK_API_SERVER not set",
)


@pytest.fixture(scope="module")
def volume_client() -> Iterator[VolumeClient]:
    with VolumeClient.from_connect(ConnectClient()) as client:
        yield client


@pytest.fixture(scope="module")
def test_volume_name() -> str:
    """Generate a unique volume name for this test run."""
    return f"a-test-volume-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def created_volume(
    volume_client: VolumeClient,
    test_volume_name: str,
) -> Iterator[Volume]:
    """Create a volume for the test session and delete it afterwards."""
    volume = volume_client.create(test_volume_name)
    assert volume.name == test_volume_name
    try:
        yield volume
    finally:
        try:
            volume.delete()
        except VolumeNotFoundError:
            pass


@needs_staging
def test_create_and_get_volume(created_volume: Volume) -> None:
    """CreateVolume then GetVolume returns matching metadata through Volume."""
    info = created_volume.reload()
    assert info.name == created_volume.name
    assert info.created_at is not None


@needs_staging
def test_list_volumes(
    volume_client: VolumeClient,
    created_volume: Volume,
) -> None:
    """ListVolumes should include our test volume."""
    volumes = volume_client.list()
    assert any(volume.name == created_volume.name for volume in volumes), (
        f"Expected {created_volume.name} in ListVolumes response"
    )
    for volume in volumes[:3]:
        assert volume.name


@needs_staging
def test_put_and_get_file(created_volume: Volume) -> None:
    """PutFile then GetFile round-trips data correctly."""
    file_path = "test-data/hello.txt"
    file_content = b"Hello from the volume e2e test!"

    created_volume.put_file(file_path, file_content)
    assert created_volume.read_file(file_path) == file_content


@needs_staging
def test_list_files(created_volume: Volume) -> None:
    """ListFiles should show files we uploaded."""
    created_volume.put_file("test-data/hello.txt", b"hello")
    paths = [f.path for f in created_volume.listdir("test-data/")]
    assert "test-data/hello.txt" in paths, f"Expected test-data/hello.txt in {paths}"


@needs_staging
def test_remove_file(created_volume: Volume) -> None:
    """RemoveFile should delete a file, and it should no longer appear in ListFiles."""
    file_path = "test-data/to-remove.txt"

    created_volume.put_file(file_path, b"delete me")
    created_volume.remove_file(file_path)
    paths = [f.path for f in created_volume.listdir("test-data/")]
    assert file_path not in paths, f"File {file_path} should have been removed"


@needs_staging
def test_delete_volume(volume_client: VolumeClient) -> None:
    """Create and delete a standalone volume to verify DeleteVolume works."""
    vol_name = f"test-delete-{uuid.uuid4().hex[:8]}"

    volume = volume_client.create(vol_name)
    volume.delete()
    with pytest.raises(VolumeNotFoundError):
        volume_client.lookup(vol_name)
