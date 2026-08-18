"""End-to-end test for generator functions.

Deploys sync and async generator functions, then verifies streaming
results via the function queue.
"""

import asyncio
import time

import chalkcompute

from e2etests._ci_resources import build_scoped_name
from e2etests.conftest import _delete_function, _deploy_script, needs_staging


def _wait_for_function(function_name: str) -> None:
    deadline = time.monotonic() + 120
    while True:
        function = chalkcompute.RemoteFunction.from_name(function_name)
        try:
            assert function.defer(0).get(timeout=10) == []
            return
        except RuntimeError as exc:
            if "No deployed scaling group serves" not in str(exc):
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


@needs_staging
class TestSyncGenerator:
    function_name = build_scoped_name("ex-count-up")

    @classmethod
    def setup_class(cls):
        _deploy_script("generator", "deploy_sync.py", function_name=cls.function_name)
        _wait_for_function(cls.function_name)

    @classmethod
    def teardown_class(cls):
        _delete_function(cls.function_name)

    def test_direct_call_iterates(self):
        """for x in fn(5) yields 5 chunks."""
        fn = chalkcompute.RemoteFunction.from_name(self.function_name)
        chunks = [batch.column("result").to_pylist() for batch in fn.remote(5)]
        assert chunks == [[0], [1], [2], [3], [4]]

    def test_defer_stream(self):
        """fn.defer(3).stream() yields 3 chunks."""
        fn = chalkcompute.RemoteFunction.from_name(self.function_name)
        handle = fn.defer(3)
        chunks = [batch.column("result").to_pylist() for batch in handle.stream()]
        assert chunks == [[0], [1], [2]]

    def test_defer_get(self):
        """fn.defer(3).get() collects all chunks."""
        fn = chalkcompute.RemoteFunction.from_name(self.function_name)
        assert fn.defer(3).get(timeout=60) == [0, 1, 2]


@needs_staging
class TestAsyncGenerator:
    function_name = build_scoped_name("ex-async-count-up")

    @classmethod
    def setup_class(cls):
        _deploy_script("generator", "deploy_async.py", function_name=cls.function_name)
        _wait_for_function(cls.function_name)

    @classmethod
    def teardown_class(cls):
        _delete_function(cls.function_name)

    def test_direct_call_async_iterates(self):
        """async for x in fn.remote(5) yields 5 chunks."""
        fn = chalkcompute.RemoteFunction.from_name(self.function_name)

        async def _collect() -> list[list[int]]:
            return [batch.column("result").to_pylist() async for batch in fn.remote(5)]

        assert asyncio.run(_collect()) == [[0], [1], [2], [3], [4]]

    def test_defer_async_stream(self):
        """fn.defer(3).stream() returns an async iterator for async functions."""
        fn = chalkcompute.RemoteFunction.from_name(self.function_name)

        async def _collect() -> list[list[int]]:
            handle = fn.defer(3)
            return [
                batch.column("result").to_pylist() async for batch in handle.stream()
            ]

        assert asyncio.run(_collect()) == [[0], [1], [2]]

    def test_defer_async_get(self):
        """handle.get() collects all chunks in sync and async contexts."""
        fn = chalkcompute.RemoteFunction.from_name(self.function_name)

        assert fn.defer(3).get(timeout=60) == [0, 1, 2]

        async def _run():
            return await fn.defer(3).get(timeout=60)

        assert asyncio.run(_run()) == [0, 1, 2]
