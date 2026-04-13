"""End-to-end tests for the Container abstraction.

Tests that Container(image=Image(...).add_local_file(..., strategy="copy")) and
Container(image=Image(...).add_local_dir(..., strategy="copy")) correctly
inline local files into the image and make them readable inside the container.
"""

import os
import tempfile
import time
import uuid
from pathlib import Path

import pytest
from dotenv import load_dotenv

from chalkcompute import Container, Image, Secret

load_dotenv()

CHALK_CLIENT_ID = os.environ.get("CHALK_CLIENT_ID", "")
CHALK_API_SERVER = os.environ.get("CHALK_API_SERVER", "")

needs_staging = pytest.mark.skipif(
    not CHALK_CLIENT_ID or not CHALK_API_SERVER,
    reason="CHALK_CLIENT_ID/CHALK_API_SERVER not set",
)


@needs_staging
def test_container_add_local_file() -> None:
    """Container with add_local_file(strategy='copy') bakes file into image."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("hello from local file")
        f.flush()
        local_path = f.name

    try:
        img = Image.debian_slim().add_local_file(local_path, "/app/data.txt", strategy="copy")
        c = Container(image=img, name=f"test-add-local-file-{uuid.uuid4().hex[:8]}")
        try:
            c.run()
            result = c.exec("cat", "/app/data.txt")
            assert result.exit_code == 0, (
                f"cat exited with code {result.exit_code}: {result.stderr_text}"
            )
            assert result.stdout_text.strip() == "hello from local file"
        finally:
            c.stop()
    finally:
        os.unlink(local_path)


@needs_staging
def test_container_run_commands_apt_install() -> None:
    """run_commands can apt-install a package that is then available in the container."""
    img = Image.debian_slim().run_commands(
        "apt-get update && apt-get install -y --no-install-recommends jq",
    )
    c = Container(image=img, name=f"test-run-commands-{uuid.uuid4().hex[:8]}")
    try:
        c.run()
        result = c.exec("jq", "--version")
        assert result.exit_code == 0, (
            f"jq --version exited with code {result.exit_code}: {result.stderr_text}"
        )
        assert result.stdout_text.strip().startswith("jq-")
    finally:
        c.stop()


@needs_staging
def test_container_from_dockerfile() -> None:
    """from_dockerfile builds an image from a Dockerfile and runs the container."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".Dockerfile", delete=False,
    ) as f:
        f.write("FROM alpine:latest\nRUN apk add --no-cache curl\n")
        f.flush()
        dockerfile_path = f.name

    try:
        img = Image.from_dockerfile(dockerfile_path)
        c = Container(image=img, name=f"test-dockerfile-{uuid.uuid4().hex[:8]}")
        try:
            c.run()
            result = c.exec("curl", "--version")
            assert result.exit_code == 0, (
                f"curl --version exited with code {result.exit_code}: {result.stderr_text}"
            )
            assert "curl" in result.stdout_text.lower()
        finally:
            c.stop()
    finally:
        os.unlink(dockerfile_path)


@needs_staging
def test_image_build_cache() -> None:
    """Building the same image twice should return immediately the second time."""
    img = Image.debian_slim().run_commands("echo cache-test")

    # First build: may take a while (actual image build)
    c1 = Container(image=img, name=f"test-cache-1-{uuid.uuid4().hex[:8]}")
    try:
        c1.run()
    finally:
        c1.stop()

    # Second build with the identical image spec: should be cached.
    # We call _build_image directly to isolate image-build time from
    # container startup time.
    c2 = Container(image=img, name=f"test-cache-2-{uuid.uuid4().hex[:8]}")
    t0 = time.monotonic()
    image_uri = c2._build_image()
    build_elapsed = time.monotonic() - t0

    assert image_uri, "Expected a non-empty image URI"
    assert build_elapsed < 10, (
        f"Cached image build took {build_elapsed:.1f}s — expected near-instant"
    )


@needs_staging
def test_container_add_local_dir() -> None:
    """Container with add_local_dir(strategy='copy') bakes files into image."""
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "greeting.txt").write_text("hello")
        sub = Path(d) / "sub"
        sub.mkdir()
        (sub / "nested.txt").write_text("world")

        img = Image.debian_slim().add_local_dir(d, "/app/mydir", strategy="copy")
        c = Container(image=img, name=f"test-add-local-dir-{uuid.uuid4().hex[:8]}")
        try:
            c.run()

            r1 = c.exec("cat", "/app/mydir/greeting.txt")
            assert r1.exit_code == 0, f"cat greeting.txt failed: {r1.stderr_text}"
            assert r1.stdout_text.strip() == "hello"

            r2 = c.exec("cat", "/app/mydir/sub/nested.txt")
            assert r2.exit_code == 0, f"cat sub/nested.txt failed: {r2.stderr_text}"
            assert r2.stdout_text.strip() == "world"
        finally:
            c.stop()


@needs_staging
def test_container_secret_from_env() -> None:
    """Secret.from_env() injects a Chalk secret as an env var into the container."""
    from chalkcompute._connect import ConnectClient

    client = ConnectClient()
    secret_name = f"TEST_SECRET_{uuid.uuid4().hex[:8].upper()}"
    secret_value = f"secret-value-{uuid.uuid4().hex[:8]}"
    _SECRETS_SVC = "chalk.server.v1.EnvironmentSecretsService"

    # Create a test secret in the environment
    resp = client.rpc(
        _SECRETS_SVC,
        "UpsertSecret",
        {"name": secret_name, "config": {"literal": secret_value}},
    )
    assert resp.status_code == 200, f"UpsertSecret failed: {resp.text}"

    try:
        img = Image.debian_slim()
        c = Container(
            image=img,
            name=f"test-secret-{uuid.uuid4().hex[:8]}",
            secrets=[Secret.from_env(secret_name)],
        )
        try:
            c.run()
            result = c.exec("sh", "-c", f"echo ${secret_name}")
            assert result.exit_code == 0, (
                f"echo ${secret_name} failed: {result.stderr_text}"
            )
            assert result.stdout_text.strip() == secret_value
        finally:
            c.stop()
    finally:
        # Clean up the test secret
        client.rpc(_SECRETS_SVC, "DeleteSecret", {"name": secret_name})


@needs_staging
def test_container_env_vars() -> None:
    """Image.env() variables are visible inside the running container."""
    img = Image.debian_slim().env({"MY_VAR": "hello_world", "ANOTHER": "42"})
    c = Container(image=img, name=f"test-env-vars-{uuid.uuid4().hex[:8]}")
    try:
        c.run()
        r1 = c.exec("sh", "-c", "echo $MY_VAR")
        assert r1.exit_code == 0, f"echo $MY_VAR failed: {r1.stderr_text}"
        assert r1.stdout_text.strip() == "hello_world"

        r2 = c.exec("sh", "-c", "echo $ANOTHER")
        assert r2.exit_code == 0, f"echo $ANOTHER failed: {r2.stderr_text}"
        assert r2.stdout_text.strip() == "42"
    finally:
        c.stop()
