"""End-to-end tests for scalar functions.

Exercises:
  - Sync/async scalar direct calls and defer/get
  - Concurrent calls (batching/coalescing under load)
  - Throughput timing (shows rate limiting effect if configured)
  - Timeout and cancellation
"""

import asyncio
import concurrent.futures
import os
import subprocess
import sys
import time

import pyarrow as pa
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


def _make_remote():
    from chalk.client.client_grpc import ChalkGRPCClient
    from chalkdf.remote import Remote

    env_id = os.environ.get("CHALK_ENVIRONMENT_ID", "tmnmc9beyujew")
    client = ChalkGRPCClient(environment=env_id)
    return Remote(chalk_client=client)


# ---------------------------------------------------------------------------
# Sync scalar
# ---------------------------------------------------------------------------


@needs_staging
class TestSyncScalar:
    @classmethod
    def setup_class(cls):
        _deploy_script("deploy_sync.py")

    def test_direct_call(self):
        """fn(1, 10) → 11 via Velox."""
        remote = _make_remote()
        fn = remote.function("test-add")
        result = fn(1, 10)
        assert result == 11

    def test_defer_get(self):
        """fn.defer(2, 20).get() → [22] via queue."""
        remote = _make_remote()
        fn = remote.function("test-add")
        handle = fn.defer(2, 20)
        result = handle.get()
        assert result.to_pylist() == [22]

    def test_concurrent_calls(self):
        """Fire 10 calls in parallel, verify all return correct results."""
        remote = _make_remote()
        fn = remote.function("test-add")
        n = 10

        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
            futures = [pool.submit(fn, i, i * 10) for i in range(1, n + 1)]
            results = [f.result() for f in futures]

        expected = [i + i * 10 for i in range(1, n + 1)]
        assert results == expected

    def test_concurrent_defer_calls(self):
        """Fire 10 deferred calls, verify all return correct results."""
        remote = _make_remote()
        fn = remote.function("test-add")
        n = 10

        handles = [fn.defer(i, i * 10) for i in range(1, n + 1)]
        results = [h.get().to_pylist()[0] for h in handles]

        expected = [i + i * 10 for i in range(1, n + 1)]
        assert results == expected

    def test_throughput(self):
        """Fire 20 rapid calls and measure throughput."""
        remote = _make_remote()
        fn = remote.function("test-add")
        n = 20

        start = time.monotonic()
        for i in range(n):
            fn.defer(i, 1).get()
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
        _deploy_script("deploy_async.py")

    def test_direct_call(self):
        """await fn(3, 30) → 33."""
        remote = _make_remote()
        fn = remote.function("test-async-add")

        async def _run():
            return await fn(3, 30)

        result = asyncio.run(_run())
        assert result == 33

    def test_defer_get(self):
        """await fn.defer(4, 40).get() → [44]."""
        remote = _make_remote()
        fn = remote.function("test-async-add")
        handle = fn.defer(4, 40)

        async def _run():
            return await handle.get()

        result = asyncio.run(_run())
        assert result.to_pylist() == [44]


# ---------------------------------------------------------------------------
# Timeout / cancellation
# ---------------------------------------------------------------------------


@needs_staging
class TestTimeout:
    def test_defer_get_timeout(self):
        """handle.get(timeout=3) on nonexistent function raises TimeoutError."""
        remote = _make_remote()
        fn = remote.function("test-add")

        # Get the raw async client to enqueue to a nonexistent function
        from libchalk.runtime.remote_call import AsyncRemoteCallClient, GrpcChannelCredentials

        client = remote._client
        stub_provider = client._stub_refresher._stub
        engine_channel = stub_provider._engine_channel
        uri = engine_channel._channel._target
        use_ssl = not (uri.startswith("localhost") or uri.startswith("127.0.0.1"))
        creds = GrpcChannelCredentials.SSL() if use_ssl else GrpcChannelCredentials.Insecure()
        metadata = {
            "x-chalk-deployment-type": "function-queue",
            "x-chalk-env-id": stub_provider.environment_id,
        }
        for interceptor in getattr(engine_channel, "_interceptors", ()):
            refresher = getattr(interceptor, "_refresher", None)
            if refresher is not None:
                metadata["authorization"] = f"Bearer {refresher.get_token().access_token}"
                break

        raw_client = AsyncRemoteCallClient.create(uri, creds, metadata)
        batch = pa.record_batch([pa.array([1])], schema=pa.schema([("a", pa.int64())]))
        call_id = raw_client.enqueue("nonexistent_fn_timeout_ci_test", batch)

        with pytest.raises(concurrent.futures.TimeoutError):
            raw_client.get_result(call_id).result(timeout=3.0)

        raw_client.cancel(call_id)
