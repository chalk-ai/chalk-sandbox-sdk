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
import uuid

import chalkcompute
import pytest
from protobuf import Oneof

from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import (
    service_connect as catalog_connect,
)
from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import service_pb as catalog_pb
from e2etests._ci_resources import build_scoped_name
from e2etests.conftest import _delete_function, _deploy_script, needs_staging


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
        cls.nested_gather_name = build_scoped_name("ex-nested-gather-add")
        _deploy_script(
            "scalar", "deploy_nested_gather.py", function_name=cls.nested_gather_name
        )

    @classmethod
    def teardown_class(cls):
        _delete_function(cls.nested_gather_name)

    def test_direct_call(self):
        """Sync scalar calls work in sync and async contexts."""
        fn = chalkcompute.RemoteFunction.from_name("ex-add")
        assert fn.remote(1, 10) == 11
        assert asyncio.run(_await_call(fn, 2, 20)) == 22

    def test_defer_get(self):
        """Sync scalar defer/get works in sync and async contexts."""
        fn = chalkcompute.RemoteFunction.from_name("ex-add")
        handle = fn.defer(2, 20)
        assert handle.get(timeout=60) == 22

        async_handle = fn.defer(3, 30)
        assert asyncio.run(_await_get(async_handle, timeout=60)) == 33

    def test_concurrent_calls(self):
        """Fire 10 calls in parallel, verify all return correct results."""
        fn = chalkcompute.RemoteFunction.from_name("ex-add")
        n = 10

        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
            futures = [pool.submit(fn.remote, i, i * 10) for i in range(1, n + 1)]
            results = [f.result() for f in futures]

        expected = [i + i * 10 for i in range(1, n + 1)]
        assert results == expected

    def test_concurrent_defer_calls(self):
        """Fire 10 deferred calls, verify all return correct results."""
        fn = chalkcompute.RemoteFunction.from_name("ex-add")
        n = 10

        handles = [fn.defer(i, i * 10) for i in range(1, n + 1)]
        results = [h.get(timeout=60) for h in handles]

        expected = [i + i * 10 for i in range(1, n + 1)]
        assert results == expected

    def test_throughput(self):
        """Fire 20 rapid calls and measure throughput."""
        fn = chalkcompute.RemoteFunction.from_name("ex-add")
        n = 20

        start = time.monotonic()
        for i in range(n):
            fn.defer(i, 1).get(timeout=60)
        elapsed = time.monotonic() - start

        throughput = n / elapsed
        print(f"\n  {n} calls in {elapsed:.2f}s ({throughput:.1f} calls/s)")
        # Just ensure it completes — no hard assertion on speed
        assert elapsed < 120, f"Took too long: {elapsed:.1f}s"

    def test_nested_gather_uses_function_queue_without_env_vars(self) -> None:
        fn = chalkcompute.RemoteFunction.from_name(self.nested_gather_name)
        assert fn.web_url, (
            f"{self.nested_gather_name} should be deployed before this test"
        )
        assert fn.remote([0, 1, 2, 3, 4, 5, 6, 7, 8, 9]) == [
            1,
            2,
            3,
            4,
            5,
            6,
            7,
            8,
            9,
            10,
        ]


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
        fn = chalkcompute.RemoteFunction.from_name("ex-async-add")

        assert fn.remote(3, 30) == 33
        assert asyncio.run(_await_call(fn, 4, 40)) == 44

    def test_defer_get(self):
        """Async scalar defer/get works in sync and async contexts."""
        fn = chalkcompute.RemoteFunction.from_name("ex-async-add")
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
        fn = chalkcompute.RemoteFunction.from_name("ex-add")
        handle = fn.defer(1, 1)
        assert handle.get(timeout=30) == 2


# ---------------------------------------------------------------------------
# Queue retries and result TTL
# ---------------------------------------------------------------------------


def _defer_when_ready(
    fn: chalkcompute.RemoteFunction, token: str
) -> chalkcompute.FunctionCallHandle:
    """Enqueue after the catalog has attached the function's scaling group."""
    deadline = time.monotonic() + 120
    while True:
        try:
            return fn.defer(token)
        except RuntimeError as exc:
            if "No deployed scaling group serves" not in str(exc):
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


def _deployed_config(fn: chalkcompute.RemoteFunction):
    """Fetch the exact configuration persisted for a deployed function."""
    assert fn.version_info is not None

    stub = fn._client.stub(catalog_connect.ExternalFunctionCatalogServiceClientSync)
    response = fn._client.call(
        stub.get_external_function_version,
        catalog_pb.GetExternalFunctionVersionRequest(
            identifier=Oneof("id", fn.version_info.id)
        ),
    )
    return response.external_function_version.config


@needs_staging
class TestFunctionRetries:
    function_name = build_scoped_name("ex-retry")

    @classmethod
    def setup_class(cls) -> None:
        _deploy_script("scalar", "deploy_retry.py", function_name=cls.function_name)

    @classmethod
    def teardown_class(cls) -> None:
        _delete_function(cls.function_name)

    def test_deployed_function_retries_failed_queue_entry(self) -> None:
        """The catalog stores the policy and retries a failed queue entry."""
        fn = chalkcompute.RemoteFunction.from_name(self.function_name)
        config = _deployed_config(fn)
        assert config.retry_policy.max_retries == 1

        token = f"retry-{uuid.uuid4().hex}"
        assert _defer_when_ready(fn, token).get(timeout=120) == f"{token}:attempt-2"


@needs_staging
class TestFunctionResultTtl:
    function_name = build_scoped_name("ex-result-ttl")

    @classmethod
    def setup_class(cls) -> None:
        _deploy_script(
            "scalar", "deploy_result_ttl.py", function_name=cls.function_name
        )

    @classmethod
    def teardown_class(cls) -> None:
        _delete_function(cls.function_name)

    def test_completed_results_expire_after_configured_ttl(self) -> None:
        """A completed deferred result becomes unavailable after its TTL."""
        fn = chalkcompute.RemoteFunction.from_name(self.function_name)
        config = _deployed_config(fn)
        assert config.queue.result_ttl_seconds == 2

        token = f"ttl-{uuid.uuid4().hex}"
        handle = _defer_when_ready(fn, token)
        assert handle.get(timeout=60) == token

        # Give Redis expiry and the queue status transition time to complete.
        time.sleep(4)
        with pytest.raises(chalkcompute.FunctionError, match="results have expired"):
            handle.get(timeout=30)
