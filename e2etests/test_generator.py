"""End-to-end test for generator functions.

Deploys sync and async generator functions, then verifies streaming
results via the function queue.
"""

import asyncio
import time
from collections.abc import Callable
from typing import TypeVar

import chalkcompute

from e2etests._ci_resources import build_scoped_name
from e2etests.conftest import (
    _delete_function,
    _delete_test_owned_source_volume,
    _deploy_test_function_source,
    needs_staging,
)

_T = TypeVar("_T")


def _retry_missing_scaling_group(call: Callable[[], _T]) -> _T:
    deadline = time.monotonic() + 120
    while True:
        try:
            return call()
        except RuntimeError as exc:
            if "No deployed scaling group serves" not in str(exc):
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


@needs_staging
class TestSyncGenerator:
    function_name = build_scoped_name("ex-count-up")
    source: tuple[chalkcompute.RemoteFunction, str | None] | None = None

    @classmethod
    def setup_class(cls):
        cls.source = _deploy_test_function_source(
            "generator", "deploy_sync.py", cls.function_name
        )

    @classmethod
    def teardown_class(cls):
        _delete_function(cls.function_name)
        if cls.source is not None and cls.source[1] is not None:
            _delete_test_owned_source_volume(cls.source[0], cls.source[1])

    def test_direct_call_iterates(self):
        """for x in fn(5) yields 5 chunks."""
        fn = chalkcompute.RemoteFunction.from_name(self.function_name)
        stream = _retry_missing_scaling_group(lambda: fn.remote(5))
        chunks = [batch.column("result").to_pylist() for batch in stream]
        assert chunks == [[0], [1], [2], [3], [4]]

    def test_defer_stream(self):
        """fn.defer(3).stream() yields 3 chunks."""
        fn = chalkcompute.RemoteFunction.from_name(self.function_name)
        handle = _retry_missing_scaling_group(lambda: fn.defer(3))
        chunks = [batch.column("result").to_pylist() for batch in handle.stream()]
        assert chunks == [[0], [1], [2]]

    def test_defer_get(self):
        """fn.defer(3).get() collects all chunks."""
        fn = chalkcompute.RemoteFunction.from_name(self.function_name)
        assert _retry_missing_scaling_group(lambda: fn.defer(3)).get(timeout=60) == [
            0,
            1,
            2,
        ]


@needs_staging
class TestAsyncGenerator:
    function_name = build_scoped_name("ex-async-count-up")
    source: tuple[chalkcompute.RemoteFunction, str | None] | None = None

    @classmethod
    def setup_class(cls):
        cls.source = _deploy_test_function_source(
            "generator", "deploy_async.py", cls.function_name
        )

    @classmethod
    def teardown_class(cls):
        _delete_function(cls.function_name)
        if cls.source is not None and cls.source[1] is not None:
            _delete_test_owned_source_volume(cls.source[0], cls.source[1])

    def test_direct_call_async_iterates(self):
        """async for x in fn.remote(5) yields 5 chunks."""
        fn = chalkcompute.RemoteFunction.from_name(self.function_name)

        async def _collect() -> list[list[int]]:
            stream = _retry_missing_scaling_group(lambda: fn.remote(5))
            return [batch.column("result").to_pylist() async for batch in stream]

        assert asyncio.run(_collect()) == [[0], [1], [2], [3], [4]]

    def test_defer_async_stream(self):
        """fn.defer(3).stream() returns an async iterator for async functions."""
        fn = chalkcompute.RemoteFunction.from_name(self.function_name)

        async def _collect() -> list[list[int]]:
            handle = _retry_missing_scaling_group(lambda: fn.defer(3))
            return [
                batch.column("result").to_pylist() async for batch in handle.stream()
            ]

        assert asyncio.run(_collect()) == [[0], [1], [2]]

    def test_defer_async_get(self):
        """handle.get() collects all chunks in sync and async contexts."""
        fn = chalkcompute.RemoteFunction.from_name(self.function_name)

        assert _retry_missing_scaling_group(lambda: fn.defer(3)).get(timeout=60) == [
            0,
            1,
            2,
        ]

        async def _run():
            handle = _retry_missing_scaling_group(lambda: fn.defer(3))
            return await handle.get(timeout=60)

        assert asyncio.run(_run()) == [0, 1, 2]
