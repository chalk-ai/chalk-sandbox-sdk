"""Volume hello-world test.

Proves that a file uploaded to a named volume is readable inside a container
that mounts it. ChalkFS may take a few seconds to sync after the container
starts, so we retry the read with a short backoff.
"""

import base64
import time
import uuid
from collections.abc import Iterator

import pytest

from chalkcompute import Container
from e2etests.conftest import _connect_rpc, needs_staging

_VOLUME_SVC = "chalk.volume.v1.VolumeService"

MOUNT_PATH = "/mnt/data"
FILE_NAME = "hello.txt"
FILE_CONTENT = "Hello from a Chalk volume!"


@pytest.fixture
def hello_vol(auth_headers: dict[str, str]) -> Iterator[str]:
    """Create a volume with hello.txt, yield its name, then delete it."""
    vol_name = f"test-hello-vol-{uuid.uuid4().hex[:8]}"

    resp = _connect_rpc(_VOLUME_SVC, "CreateVolume", {"name": vol_name}, auth_headers)
    resp.raise_for_status()
    resp = _connect_rpc(
        _VOLUME_SVC,
        "PutFile",
        {
            "volumeName": vol_name,
            "path": FILE_NAME,
            "data": base64.b64encode(FILE_CONTENT.encode()).decode(),
        },
        auth_headers,
    )
    resp.raise_for_status()

    yield vol_name

    _connect_rpc(_VOLUME_SVC, "DeleteVolume", {"name": vol_name}, auth_headers)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@needs_staging
def test_file_readable_in_container(hello_vol: str) -> None:
    """A file uploaded to a volume is visible at the mount path inside a container."""
    container = Container(
        image="python:3.14-slim-trixie",
        volumes=[(hello_vol, MOUNT_PATH)],
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
