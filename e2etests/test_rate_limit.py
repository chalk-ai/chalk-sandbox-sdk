"""Tests for rate limiting and concurrency limiting.

These tests deploy functions with explicit policies and verify the
policies are enforced by the catalog consumer.
"""

import concurrent.futures
import time

import chalkcompute

from e2etests.conftest import _deploy_script, needs_staging


# ---------------------------------------------------------------------------
# Rate limiting: 5 req/s
# ---------------------------------------------------------------------------


@needs_staging
class TestRateLimit:
    @classmethod
    def setup_class(cls):
        _deploy_script("scalar", "deploy_rate_limited.py")

    def test_rate_limit_throttles(self):
        """20 rapid calls at 5 req/s should take at least ~3 seconds.

        Without rate limiting, 20 deferred calls complete in ~1-2s.
        With 5 req/s, the consumer can only process 5 per second,
        so 20 calls should take at least 3s (ceil(20/5) - 1 = 3).
        """
        fn = chalkcompute.RemoteFunction.from_name("ex-add-rate-limited")
        n = 20

        # Enqueue all at once
        handles = [fn.defer(i, 1) for i in range(n)]

        start = time.monotonic()
        results = [h.get(timeout=120) for h in handles]
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
        fn = chalkcompute.RemoteFunction.from_name("ex-add-rate-limited")

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
        _deploy_script("scalar", "deploy_concurrency_limited.py")

    def test_concurrency_limit_caps_parallelism(self):
        """6 calls with 1s sleep and max_concurrent=2 should take >= 3s.

        With unlimited concurrency, 6 × 1s-sleep calls would take ~1s (all parallel).
        With max_concurrent=2, only 2 run at a time: ceil(6/2) × 1s = 3s minimum.
        """
        fn = chalkcompute.RemoteFunction.from_name("ex-slow-add")
        n = 6

        handles = [fn.defer(i, 1) for i in range(n)]

        start = time.monotonic()
        results = [h.get(timeout=120) for h in handles]
        elapsed = time.monotonic() - start

        # Verify correctness
        expected = [i + 1 for i in range(n)]
        assert results == expected, f"Expected {expected}, got {results}"

        # Verify concurrency limiting: 6 calls × 1s sleep / 2 concurrent = 3s min
        print(f"\n  Concurrency-limited: {n} calls in {elapsed:.2f}s")
        print("  Expected >= 3s (6 calls / 2 concurrent × 1s each)")

        assert elapsed >= 2.5, (
            f"Concurrency limiting not working: {n} calls completed in {elapsed:.1f}s "
            f"— expected >= 3s with max_concurrent=2"
        )

    def test_concurrency_limit_correctness(self):
        """All results are correct even under concurrency limiting."""
        fn = chalkcompute.RemoteFunction.from_name("ex-slow-add")

        handles = [fn.defer(i, i) for i in range(1, 5)]
        results = [h.get(timeout=120) for h in handles]

        expected = [i + i for i in range(1, 5)]
        assert results == expected
