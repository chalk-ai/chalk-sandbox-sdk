"""End-to-end test for generator functions.

Deploys sync and async generator functions, then verifies streaming
results via the function queue.
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

needs_staging = pytest.mark.skipif(
    not CHALK_CLIENT_ID or not CHALK_API_SERVER,
    reason="CHALK_CLIENT_ID/CHALK_API_SERVER not set",
)

EXAMPLE_DIR = os.path.dirname(__file__)


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
class TestSyncGenerator:
    @classmethod
    def setup_class(cls):
        _deploy_script("deploy.py")

    def test_direct_call_iterates(self):
        """for x in fn(5) yields 5 chunks."""
        from chalkdf.remote import Remote

        remote = Remote()
        fn = remote.function("count-up")

        chunks = []
        for batch in fn(5):
            chunks.append(batch.column("result").to_pylist())

        assert len(chunks) == 5
        assert chunks == [[0], [1], [2], [3], [4]]

    def test_defer_stream(self):
        """fn.defer(3).stream() yields 3 chunks."""
        from chalkdf.remote import Remote

        remote = Remote()
        fn = remote.function("count-up")
        handle = fn.defer(3)

        chunks = []
        for batch in handle.stream():
            chunks.append(batch.column("result").to_pylist())

        assert len(chunks) == 3
        assert chunks == [[0], [1], [2]]

    def test_defer_get(self):
        """fn.defer(3).get() collects all chunks."""
        from chalkdf.remote import Remote

        remote = Remote()
        fn = remote.function("count-up")
        handle = fn.defer(3)
        result = handle.get()

        assert result is not None


@needs_staging
class TestAsyncGenerator:
    def test_direct_call_async_iterates(self):
        """async for x in fn(5) yields 5 chunks."""
        from chalkdf.remote import Remote

        remote = Remote()
        fn = remote.function("async-count-up")

        chunks = []

        async def _run():
            async for batch in fn(5):
                chunks.append(batch.column("result").to_pylist())

        asyncio.run(_run())

        assert len(chunks) == 5
        assert chunks == [[0], [1], [2], [3], [4]]

    def test_defer_async_stream(self):
        """fn.defer(3).stream() returns an async iterator for async functions."""
        from chalkdf.remote import Remote

        remote = Remote()
        fn = remote.function("async-count-up")
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
        from chalkdf.remote import Remote

        remote = Remote()
        fn = remote.function("async-count-up")
        handle = fn.defer(3)

        async def _run():
            return await handle.get()

        result = asyncio.run(_run())
        assert result is not None
