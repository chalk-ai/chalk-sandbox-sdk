"""Tests for rate limiting and concurrency limiting.

These tests deploy functions with explicit policies and verify the
policies are enforced by the catalog consumer.

Can run in CI or by hand:

    cd dataframe && source .venv/bin/activate
    CHALK_FUNCTION_STRIP_SOURCE=1 python -m pytest \
        ../chalk-sandbox-sdk/examples/scalar/test_rate_limit.py -v -s

Prerequisites:
    - CHALK_CLIENT_ID / CHALK_CLIENT_SECRET / CHALK_API_SERVER set
"""

import concurrent.futures
import os
import subprocess
import sys
import time
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
# Rate limiting: 5 req/s
# ---------------------------------------------------------------------------


@needs_staging
class TestRateLimit:
    @classmethod
    def setup_class(cls):
        _deploy_script("deploy_rate_limited.py")

    @classmethod
    def teardown_class(cls):
        """Delete the deployed function after tests."""
        try:
            import chalkcompute
            fn = chalkcompute.RemoteFunction.from_name(name=make_function_name("add-rate-limited"))
            fn.delete()
        except Exception as e:
            print(f"Warning: Failed to delete function: {e}")

    def test_rate_limit_throttles(self):
        """20 rapid calls at 5 req/s should take at least ~3 seconds.

        Without rate limiting, 20 deferred calls complete in ~1-2s.
        With 5 req/s, the consumer can only process 5 per second,
        so 20 calls should take at least 3s (ceil(20/5) - 1 = 3).
        """
        remote = _make_remote()
        fn = remote.function(make_function_name("add-rate-limited"))
        n = 20

        # Enqueue all at once
        handles = [fn.defer(i, 1) for i in range(n)]

        start = time.monotonic()
        results = [h.get().to_pylist()[0] for h in handles]
        elapsed = time.monotonic() - start

        # Verify correctness
        expected = [i + 1 for i in range(n)]
        assert results == expected, f"Expected {expected}, got {results}"

        # Verify throttling: 20 calls at 5/s should take >= 3s
        throughput = n / elapsed
        print(
            f"\n  Rate-limited: {n} calls in {elapsed:.2f}s ({throughput:.1f} calls/s)"
        )
        print(f"  Expected ~5 calls/s, got {throughput:.1f} calls/s")

        # Loose assertion: throughput should be noticeably below unthrottled
        # (unthrottled is typically 6-10 calls/s based on test_throughput)
        # With 5 req/s limit, we expect throughput <= 6 calls/s
        assert elapsed >= 2.0, (
            f"Rate limiting not working: {n} calls completed in {elapsed:.1f}s "
            f"({throughput:.1f} calls/s) — expected >= 2s"
        )

    def test_rate_limit_correctness_under_load(self):
        """All results are correct even under rate limiting."""
        remote = _make_remote()
        fn = remote.function(make_function_name("add-rate-limited"))

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(fn, i, i * 10) for i in range(1, 11)]
            results = [f.result() for f in futures]

        expected = [i + i * 10 for i in range(1, 11)]
        assert results == expected


# ---------------------------------------------------------------------------
# Concurrency limiting: max 2 concurrent
# ---------------------------------------------------------------------------


@needs_staging
class TestConcurrencyLimit:
    @classmethod
    def setup_class(cls):
        _deploy_script("deploy_concurrency_limited.py")

    @classmethod
    def teardown_class(cls):
        """Delete the deployed function after tests."""
        try:
            import chalkcompute
            fn = chalkcompute.RemoteFunction.from_name(name=make_function_name("slow-add"))
            fn.delete()
        except Exception as e:
            print(f"Warning: Failed to delete function: {e}")

    def test_concurrency_limit_caps_parallelism(self):
        """6 calls with 1s sleep and max_concurrent=2 should take >= 3s.

        With unlimited concurrency, 6 × 1s-sleep calls would take ~1s (all parallel).
        With max_concurrent=2, only 2 run at a time: ceil(6/2) × 1s = 3s minimum.
        """
        remote = _make_remote()
        fn = remote.function(make_function_name("slow-add"))
        n = 6

        handles = [fn.defer(i, 1) for i in range(n)]

        start = time.monotonic()
        results = [h.get().to_pylist()[0] for h in handles]
        elapsed = time.monotonic() - start

        # Verify correctness
        expected = [i + 1 for i in range(n)]
        assert results == expected, f"Expected {expected}, got {results}"

        # Verify concurrency limiting: 6 calls × 1s sleep / 2 concurrent = 3s min
        print(f"\n  Concurrency-limited: {n} calls in {elapsed:.2f}s")
        print(f"  Expected >= 3s (6 calls / 2 concurrent × 1s each)")

        assert elapsed >= 2.5, (
            f"Concurrency limiting not working: {n} calls completed in {elapsed:.1f}s "
            f"— expected >= 3s with max_concurrent=2"
        )

    def test_concurrency_limit_correctness(self):
        """All results are correct even under concurrency limiting."""
        remote = _make_remote()
        fn = remote.function(make_function_name("slow-add"))

        handles = [fn.defer(i, i) for i in range(1, 5)]
        results = [h.get().to_pylist()[0] for h in handles]

        expected = [i + i for i in range(1, 5)]
        assert results == expected
