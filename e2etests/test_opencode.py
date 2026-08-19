"""End-to-end tests for the opencode example.

Proves that:
  1. A container can be stood up and the opencode server started successfully.
  2. The server's public web URL is reachable from outside the container.
"""

import sys
import uuid
from collections.abc import Generator

from pyqwest import SyncClient
import pytest
from chalkcompute import Container

from e2etests.conftest import EXAMPLES_DIR, needs_staging

pytestmark = pytest.mark.skip(reason="expensive — example-app integration smoke")

# Import helpers directly from the example script.
sys.path.insert(0, str(EXAMPLES_DIR / "opencode"))
from opencode import OPENCODE_IMAGE, OPENCODE_PORT, clone_repo, start_server  # noqa: E402

# A tiny public repo to clone — avoids needing GITHUB_TOKEN in CI.
TEST_REPO = "octocat/Hello-World"
TEST_REF = "master"


@pytest.fixture(scope="module")
def opencode_container() -> Generator[Container, None, None]:
    """Start a container, install opencode, clone the test repo, and start the server.

    Mirrors exactly what the example script does in main(), so a passing fixture
    means the full user-facing flow works end-to-end.
    """
    name = f"test-opencode-{uuid.uuid4().hex[:8]}"
    container = Container(
        image=OPENCODE_IMAGE,
        name=name,
        port=OPENCODE_PORT,
        entrypoint=[
            "bash",
            "-c",
            f"python3 -m http.server {OPENCODE_PORT} --bind 0.0.0.0 &>/dev/null"
            f" & echo $! > /tmp/http_server.pid; tail -f /dev/null",
        ],
    ).run()
    try:
        clone_repo(container, TEST_REPO, TEST_REF, token=None)
        start_server(container)
    except Exception:
        container.stop()
        raise
    yield container
    container.stop()


@needs_staging
def test_server_responds_on_localhost(opencode_container: Container) -> None:
    """opencode serve is reachable on localhost inside the container."""
    result = opencode_container.exec(
        "bash",
        "-c",
        f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{OPENCODE_PORT}",
    )
    assert result.exit_code == 0, f"curl failed (exit {result.exit_code})"
    status = result.stdout_text.strip()
    # Any non-5xx response proves the server is handling requests.
    assert status.isdigit() and int(status) < 500, (
        f"Expected a non-5xx HTTP status from opencode, got: {status!r}"
    )


@needs_staging
def test_public_web_url_is_accessible(opencode_container: Container) -> None:
    """The container's public web URL responds to requests from outside the container."""
    web_url = opencode_container.info.web_url if opencode_container.info else None
    assert web_url, (
        "No web_url returned — the container port may not be publicly exposed"
    )
    resp = SyncClient().get(web_url, timeout=15)
    assert resp.status < 500, f"Server at {web_url} returned {resp.status}"
