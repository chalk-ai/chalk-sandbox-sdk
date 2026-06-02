"""End-to-end tests for scalar functions.

Exercises:
  - Sync/async scalar direct calls and defer/get
  - Concurrent calls (batching/coalescing under load)
  - Throughput timing (shows rate limiting effect if configured)
  - Timeout and cancellation
"""

import asyncio
import concurrent.futures
import time

import chalkcompute
from e2etests.conftest import _deploy_script, needs_staging


async def _await_call(fn, *args):
    return await fn.remote(*args)


async def _await_get(handle, *, timeout=None):
    return await handle.get(timeout=timeout)


# ---------------------------------------------------------------------------
# Sync scalar
# ---------------------------------------------------------------------------


@needs_staging
class TestSyncScalar:
    @classmethod
    def setup_class(cls):
        _deploy_script("scalar", "deploy_sync.py")

    def test_direct_call(self):
        """Sync scalar calls work in sync and async contexts."""
        fn = chalkcompute.RemoteFunction.from_name("add")
        assert fn.remote(1, 10) == 11
        assert asyncio.run(_await_call(fn, 2, 20)) == 22

    def test_defer_get(self):
        """Sync scalar defer/get works in sync and async contexts."""
        fn = chalkcompute.RemoteFunction.from_name("add")
        handle = fn.defer(2, 20)
        assert handle.get(timeout=60) == 22

        async_handle = fn.defer(3, 30)
        assert asyncio.run(_await_get(async_handle, timeout=60)) == 33

    def test_concurrent_calls(self):
        """Fire 10 calls in parallel, verify all return correct results."""
        fn = chalkcompute.RemoteFunction.from_name("add")
        n = 10

        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
            futures = [pool.submit(fn.remote, i, i * 10) for i in range(1, n + 1)]
            results = [f.result() for f in futures]

        expected = [i + i * 10 for i in range(1, n + 1)]
        assert results == expected

    def test_concurrent_defer_calls(self):
        """Fire 10 deferred calls, verify all return correct results."""
        fn = chalkcompute.RemoteFunction.from_name("add")
        n = 10

        handles = [fn.defer(i, i * 10) for i in range(1, n + 1)]
        results = [h.get(timeout=60) for h in handles]

        expected = [i + i * 10 for i in range(1, n + 1)]
        assert results == expected

    def test_throughput(self):
        """Fire 20 rapid calls and measure throughput."""
        fn = chalkcompute.RemoteFunction.from_name("add")
        n = 20

        start = time.monotonic()
        for i in range(n):
            fn.defer(i, 1).get(timeout=60)
        elapsed = time.monotonic() - start

        throughput = n / elapsed
        print(f"\n  {n} calls in {elapsed:.2f}s ({throughput:.1f} calls/s)")
        # Just ensure it completes — no hard assertion on speed
        assert elapsed < 120, f"Took too long: {elapsed:.1f}s"


# ---------------------------------------------------------------------------
# Async scalar
# ---------------------------------------------------------------------------


@needs_staging
class TestAsyncScalar:
    @classmethod
    def setup_class(cls):
        _deploy_script("scalar", "deploy_async.py")

    def test_direct_call(self):
        """Async scalar calls work in sync and async contexts."""
        fn = chalkcompute.RemoteFunction.from_name("async_add")

        assert fn.remote(3, 30) == 33
        assert asyncio.run(_await_call(fn, 4, 40)) == 44

    def test_defer_get(self):
        """Async scalar defer/get works in sync and async contexts."""
        fn = chalkcompute.RemoteFunction.from_name("async_add")
        handle = fn.defer(5, 50)
        assert handle.get(timeout=60) == 55

        async_handle = fn.defer(6, 60)
        assert asyncio.run(_await_get(async_handle, timeout=60)) == 66


# ---------------------------------------------------------------------------
# Timeout / cancellation
# ---------------------------------------------------------------------------


@needs_staging
class TestTimeout:
    def test_defer_get_with_timeout(self):
        """handle.get(timeout=30) succeeds for a real function."""
        fn = chalkcompute.RemoteFunction.from_name("add")
        handle = fn.defer(1, 1)
        assert handle.get(timeout=30) == 2
