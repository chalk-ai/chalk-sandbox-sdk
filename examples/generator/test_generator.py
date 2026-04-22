"""End-to-end test for generator functions.

Deploys sync and async generator functions, then verifies streaming
results via the function queue.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv()

CHALK_CLIENT_ID = os.environ.get("CHALK_CLIENT_ID", "")
CHALK_API_SERVER = os.environ.get("CHALK_API_SERVER", "")

needs_staging = pytest.mark.skipif(
    not CHALK_CLIENT_ID or not CHALK_API_SERVER,
    reason="CHALK_CLIENT_ID/CHALK_API_SERVER not set",
)

EXAMPLE_DIR = os.path.dirname(__file__)

# Add examples dir to path so we can import test_utils
sys.path.insert(0, str(Path(__file__).parent.parent))
from test_utils import make_function_name

CHALK_ENVIRONMENT_ID = os.environ.get("CHALK_ENVIRONMENT_ID", "tmnmc9beyujew")


def _make_remote():
    from chalk.client.client_grpc import ChalkGRPCClient
    from chalkdf.remote import Remote

    client = ChalkGRPCClient(environment=CHALK_ENVIRONMENT_ID)
    return Remote(chalk_client=client)


def _deploy_script(script_name: str, function_name: str | None = None) -> None:
    """Run a deploy script as a subprocess."""
    cmd = [sys.executable, os.path.join(EXAMPLE_DIR, script_name)]
    if function_name:
        cmd.extend(["--function-name", function_name])
    result = subprocess.run(
        cmd,
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
class TestSyncGenerator:
    @classmethod
    def setup_class(cls):
        _deploy_script("deploy_sync.py", make_function_name("count-up"))

    @classmethod
    def teardown_class(cls):
        """Delete the deployed function after tests."""
        try:
            import chalkcompute
            fn = chalkcompute.RemoteFunction.from_name(name=make_function_name("count-up"))
            fn.delete()
        except Exception as e:
            print(f"Warning: Failed to delete function: {e}")

    def test_direct_call_iterates(self):
        """for x in fn(5) yields 5 chunks."""
        remote = _make_remote()
        fn = remote.function(make_function_name("count-up"))

        chunks = []
        for batch in fn(5):
            chunks.append(batch.column("result").to_pylist())

        assert len(chunks) == 5
        assert chunks == [[0], [1], [2], [3], [4]]

    def test_defer_stream(self):
        """fn.defer(3).stream() yields 3 chunks."""
        remote = _make_remote()
        fn = remote.function(make_function_name("count-up"))
        handle = fn.defer(3)

        chunks = []
        for batch in handle.stream():
            chunks.append(batch.column("result").to_pylist())

        assert len(chunks) == 3
        assert chunks == [[0], [1], [2]]

    def test_defer_get(self):
        """fn.defer(3).get() collects all chunks."""
        remote = _make_remote()
        fn = remote.function(make_function_name("count-up"))
        handle = fn.defer(3)
        result = handle.get()

        assert result is not None


@needs_staging
class TestAsyncGenerator:
    @classmethod
    def setup_class(cls):
        _deploy_script("deploy_async.py", make_function_name("async-count-up"))

    @classmethod
    def teardown_class(cls):
        """Delete the deployed function after tests."""
        try:
            import chalkcompute
            fn = chalkcompute.RemoteFunction.from_name(name=make_function_name("async-count-up"))
            fn.delete()
        except Exception as e:
            print(f"Warning: Failed to delete function: {e}")

    def test_direct_call_async_iterates(self):
        """async for x in fn(5) yields 5 chunks."""
        remote = _make_remote()
        fn = remote.function(make_function_name("async-count-up"))

        chunks = []

        async def _run():
            async for batch in fn(5):
                chunks.append(batch.column("result").to_pylist())

        asyncio.run(_run())

        assert len(chunks) == 5
        assert chunks == [[0], [1], [2], [3], [4]]

    def test_defer_async_stream(self):
        """fn.defer(3).stream() returns an async iterator for async functions."""
        remote = _make_remote()
        fn = remote.function(make_function_name("async-count-up"))
        handle = fn.defer(3)

        chunks = []

        async def _run():
            async for batch in handle.stream():
                chunks.append(batch.column("result").to_pylist())

        asyncio.run(_run())

        assert len(chunks) == 3
        assert chunks == [[0], [1], [2]]

    def test_defer_async_get(self):
        """await handle.get() collects all chunks for async functions."""
        remote = _make_remote()
        fn = remote.function(make_function_name("async-count-up"))
        handle = fn.defer(3)

        async def _run():
            return await handle.get()

        result = asyncio.run(_run())
        assert result is not None
