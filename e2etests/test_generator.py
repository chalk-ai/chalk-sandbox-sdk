"""End-to-end test for generator functions.

Deploys sync and async generator functions, then verifies streaming
results via the function queue.
"""

import asyncio

import chalkcompute

from e2etests.conftest import _deploy_script, needs_staging


@needs_staging
class TestSyncGenerator:
    @classmethod
    def setup_class(cls):
        _deploy_script("generator", "deploy_sync.py")

    def test_direct_call_iterates(self):
        """for x in fn(5) yields 5 chunks."""
        fn = chalkcompute.RemoteFunction.from_name("count_up")
        chunks = [batch.column("result").to_pylist() for batch in fn(5)]
        assert chunks == [[0], [1], [2], [3], [4]]

    def test_defer_stream(self):
        """fn.defer(3).stream() yields 3 chunks."""
        fn = chalkcompute.RemoteFunction.from_name("count_up")
        handle = fn.defer(3)
        chunks = [batch.column("result").to_pylist() for batch in handle.stream()]
        assert chunks == [[0], [1], [2]]

    def test_defer_get(self):
        """fn.defer(3).get() collects all chunks."""
        fn = chalkcompute.RemoteFunction.from_name("count_up")
        assert fn.defer(3).get(timeout=60) == [0, 1, 2]


@needs_staging
class TestAsyncGenerator:
    @classmethod
    def setup_class(cls):
        _deploy_script("generator", "deploy_async.py")

    def test_direct_call_async_iterates(self):
        """async for x in fn(5) yields 5 chunks."""
        fn = chalkcompute.RemoteFunction.from_name("async_count_up")

        async def _collect() -> list[list[int]]:
            return [batch.column("result").to_pylist() async for batch in fn(5)]

        assert asyncio.run(_collect()) == [[0], [1], [2], [3], [4]]

    def test_defer_async_stream(self):
        """fn.defer(3).stream() returns an async iterator for async functions."""
        fn = chalkcompute.RemoteFunction.from_name("async_count_up")

        async def _collect() -> list[list[int]]:
            handle = fn.defer(3)
            return [batch.column("result").to_pylist() async for batch in handle.stream()]

        assert asyncio.run(_collect()) == [[0], [1], [2]]

    def test_defer_async_get(self):
        """await handle.get() collects all chunks for async functions."""
        fn = chalkcompute.RemoteFunction.from_name("async_count_up")

        async def _run():
            return await fn.defer(3).get(timeout=60)

        assert asyncio.run(_run()) == [0, 1, 2]
