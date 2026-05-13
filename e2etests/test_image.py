"""Integration tests for Image building against the staging sandbox service.

These tests require CHALK_CLIENT_ID/CHALK_API_SERVER env vars.
Unit tests have been moved to tests/test_image.py.
"""

import os
import uuid
from collections.abc import Iterator

import pytest
from dotenv import load_dotenv

from chalkcompute import Image, SandboxClient, build_image

load_dotenv()

CHALK_CLIENT_ID = os.environ.get("CHALK_CLIENT_ID", "")
CHALK_API_SERVER = os.environ.get("CHALK_API_SERVER", "")

POLL_INTERVAL = 0.5
POLL_TIMEOUT = 600


# ---------------------------------------------------------------------------
# Integration tests: require CHALK_* env vars
# ---------------------------------------------------------------------------

needs_staging = pytest.mark.skipif(
    not CHALK_CLIENT_ID or not CHALK_API_SERVER,
    reason="CHALK_CLIENT_ID/CHALK_API_SERVER not set",
)


@pytest.fixture(scope="module")
def sandbox_client() -> Iterator[SandboxClient]:
    with SandboxClient.from_env() as client:
        yield client


@needs_staging
def test_list_sandboxes(sandbox_client: SandboxClient) -> None:
    """Verify SandboxClient authenticates and lists sandboxes."""
    sandboxes = sandbox_client.list()
    assert isinstance(sandboxes, list)
    for sandbox in sandboxes:
        assert sandbox.id
        assert sandbox.status


@needs_staging
def test_create_sandbox_with_debian_image(sandbox_client: SandboxClient) -> None:
    """Create a sandbox with a debian_slim Image using SandboxClient."""
    img = (
        Image.debian_slim("3.12")
        .pip_install(["requests"])
        .run_commands("echo 'image built successfully'")
        .workdir("/home/user")
        .env({"TEST_VAR": "hello"})
    )

    sandbox = sandbox_client.create(
        img,
        name=f"test-debian-slim-{uuid.uuid4().hex[:8]}",
        poll_interval=POLL_INTERVAL,
        timeout=POLL_TIMEOUT,
    )
    try:
        assert sandbox.info.status == "ready"
    finally:
        sandbox.terminate()


@needs_staging
def test_create_sandbox_with_plain_image(sandbox_client: SandboxClient) -> None:
    """Create a sandbox from an image string using SandboxClient."""
    sandbox = sandbox_client.create(
        "python:3.12-slim-bookworm",
        name=f"test-plain-image-{uuid.uuid4().hex[:8]}",
        poll_interval=POLL_INTERVAL,
        timeout=POLL_TIMEOUT,
    )
    try:
        assert sandbox.info.status == "ready"
    finally:
        sandbox.terminate()


@needs_staging
def test_build_custom_image() -> None:
    """Build a custom image via the public SDK helper."""
    img = (
        Image.debian_slim("3.12")
        .pip_install(["requests"])
        .run_commands("echo 'custom image built'")
    )

    image_uri = build_image(img)
    assert image_uri
