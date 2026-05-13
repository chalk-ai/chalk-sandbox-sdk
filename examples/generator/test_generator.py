"""End-to-end test for generator functions.

Deploys sync and async generator functions, then verifies streaming
results via the function queue.
"""

import asyncio
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from typing import TypeVar

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

RUN_ID = os.environ.get("CHALK_TEST_RUN_ID", uuid.uuid4().hex[:8])
SYNC_FUNCTION_NAME = f"count-up-{RUN_ID}"
ASYNC_FUNCTION_NAME = f"async-count-up-{RUN_ID}"
T = TypeVar("T")


def _make_remote_function(name: str):
    from chalkcompute import RemoteFunction

    return RemoteFunction.from_name(name).wait_ready(timeout=120)


def _deploy_script(script_name: str, extra_env: dict[str, str]) -> None:
    """Run a deploy script as a subprocess."""
    result = subprocess.run(
        [sys.executable, os.path.join(EXAMPLE_DIR, script_name)],
        capture_output=True,
        text=True,
        env={**os.environ, **extra_env},
        timeout=600,
    )
    if result.returncode != 0:
        pytest.fail(
            f"{script_name} deploy failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout[-500:]}\n"
            f"stderr: {result.stderr[-500:]}"
        )


def _is_queue_proxy_error(exc: RuntimeError) -> bool:
    message = str(exc)
    return "ProxyError" in message and "CallFunction RPC failed" in message


def _retry_queue_proxy_call(call: Callable[[], T]) -> T:
    for attempt in range(3):
        try:
            return call()
        except RuntimeError as exc:
            if not _is_queue_proxy_error(exc) or attempt == 2:
                raise
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


@needs_staging
class TestSyncGenerator:
    @classmethod
    def setup_class(cls):
        _deploy_script(
            "deploy_sync.py",
            {"CHALK_SYNC_GENERATOR_FUNCTION_NAME": SYNC_FUNCTION_NAME},
        )
        cls.function = _make_remote_function(SYNC_FUNCTION_NAME)

    def test_direct_call_iterates(self):
        """for x in fn(5) yields 5 chunks."""
        fn = self.function

        def _collect() -> list[list[int]]:
            chunks = []
            for batch in fn(5):
                chunks.append(batch.column("result").to_pylist())
            return chunks

        chunks = _retry_queue_proxy_call(_collect)

        assert len(chunks) == 5
        assert chunks == [[0], [1], [2], [3], [4]]

    def test_defer_stream(self):
        """fn.defer(3).stream() yields 3 chunks."""
        fn = self.function

        def _collect() -> list[list[int]]:
            handle = fn.defer(3)
            chunks = []
            for batch in handle.stream():
                chunks.append(batch.column("result").to_pylist())
            return chunks

        chunks = _retry_queue_proxy_call(_collect)

        assert len(chunks) == 3
        assert chunks == [[0], [1], [2]]

    def test_defer_get(self):
        """fn.defer(3).get() collects all chunks."""
        fn = self.function
        result = _retry_queue_proxy_call(lambda: fn.defer(3).get())

        assert result == [0, 1, 2]


@needs_staging
class TestAsyncGenerator:
    @classmethod
    def setup_class(cls):
        _deploy_script(
            "deploy_async.py",
            {"CHALK_ASYNC_GENERATOR_FUNCTION_NAME": ASYNC_FUNCTION_NAME},
        )
        cls.function = _make_remote_function(ASYNC_FUNCTION_NAME)

    def test_direct_call_async_iterates(self):
        """async for x in fn(5) yields 5 chunks."""
        fn = self.function

        async def _collect() -> list[list[int]]:
            chunks = []
            async for batch in fn(5):
                chunks.append(batch.column("result").to_pylist())
            return chunks

        chunks = _retry_queue_proxy_call(lambda: asyncio.run(_collect()))

        assert len(chunks) == 5
        assert chunks == [[0], [1], [2], [3], [4]]

    def test_defer_async_stream(self):
        """fn.defer(3).stream() returns an async iterator for async functions."""
        fn = self.function

        async def _collect() -> list[list[int]]:
            handle = fn.defer(3)
            chunks = []
            async for batch in handle.stream():
                chunks.append(batch.column("result").to_pylist())
            return chunks

        chunks = _retry_queue_proxy_call(lambda: asyncio.run(_collect()))

        assert len(chunks) == 3
        assert chunks == [[0], [1], [2]]

    def test_defer_async_get(self):
        """await handle.get() collects all chunks for async functions."""
        fn = self.function

        async def _run():
            handle = fn.defer(3)
            return await handle.get()

        result = _retry_queue_proxy_call(lambda: asyncio.run(_run()))
        assert result == [0, 1, 2]
