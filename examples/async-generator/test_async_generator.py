"""End-to-end test for the async-generator example.

Deploys the ``count-up`` async-generator function and streams values from it
via ``chalkdf.Remote``. Exercises the ``async for`` iterator path added in
chalk-private commit 8cbf5895a9f76c453c271ac933dad84bc9e1a7db.
"""

import asyncio
import os
import subprocess
import sys

import pytest
from dotenv import load_dotenv

load_dotenv()

CHALK_CLIENT_ID = os.environ.get("CHALK_CLIENT_ID", "")
CHALK_API_SERVER = os.environ.get("CHALK_API_SERVER", "")
CHALK_ENVIRONMENT_ID = os.environ.get("CHALK_ENVIRONMENT_ID", "")

needs_staging = pytest.mark.skipif(
    not CHALK_CLIENT_ID or not CHALK_API_SERVER or not CHALK_ENVIRONMENT_ID,
    reason="CHALK_CLIENT_ID/CHALK_API_SERVER/CHALK_ENVIRONMENT_ID not set",
)


def _chalkdf_remote_available() -> bool:
    try:
        import chalkdf.remote  # noqa: F401
    except ImportError:
        return False
    return True


needs_chalkdf_remote = pytest.mark.skipif(
    not _chalkdf_remote_available(),
    reason="chalkdf.remote not available (requires a chalkdf release that includes it)",
)

EXAMPLE_DIR = os.path.dirname(__file__)
N = 5


def _deploy_script(script_name: str) -> None:
    """Run a deploy script as a subprocess."""
    result = subprocess.run(
        [sys.executable, os.path.join(EXAMPLE_DIR, script_name)],
        capture_output=True,
        text=True,
        env=os.environ,
        timeout=600,
    )
    if result.returncode != 0:
        pytest.fail(
            f"{script_name} deploy failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout[-500:]}\n"
            f"stderr: {result.stderr[-500:]}"
        )


@needs_staging
@needs_chalkdf_remote
def test_async_generator_stream_via_remote() -> None:
    """Deploy count-up, then stream its yielded values via chalkdf.Remote."""
    _deploy_script("count_up.py")

    import pyarrow as pa
    from chalk.client.client_grpc import ChalkGRPCClient
    from chalkdf.remote import Remote

    async def _collect() -> list[int]:
        client = ChalkGRPCClient()
        remote = Remote(chalk_client=client)
        fn = remote.function("count-up", output_type=pa.int64())
        collected: list[int] = []
        async for batch in fn(N):
            for value in batch.column(0).to_pylist():
                collected.append(value)
        return collected

    values = asyncio.run(_collect())
    assert values == list(range(N)), f"Expected {list(range(N))}, got {values}"
