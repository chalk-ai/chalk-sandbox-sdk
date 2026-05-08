"""Tests for policy dataclasses (rate limit / concurrency / retry) and the
deploy-time guards that reject generator handlers + max_batching_size.

The ``to_dict()`` output of each policy is the wire contract — the dict
ends up in ``function_config`` on CreateExternalFunctionVersion, so these
tests lock in the key names the server reads.
"""

from __future__ import annotations

import pytest

from chalkcompute._cls import ClassError, RemoteClass, method
from chalkcompute._concurrency import ConcurrencyPolicy
from chalkcompute._function import FunctionError, RemoteFunction
from chalkcompute._rate_limit import RateLimitPolicy
from chalkcompute._retry import RetryPolicy


class TestRateLimitPolicyToDict:
    def test_rate_only_defaults_per_to_second(self):
        assert RateLimitPolicy(rate=10).to_dict() == {"rate": 10, "per": "second"}

    def test_custom_per_unit(self):
        assert RateLimitPolicy(rate=100, per="minute").to_dict() == {
            "rate": 100,
            "per": "minute",
        }

    def test_key_included_when_set(self):
        assert RateLimitPolicy(rate=5, per="hour", key="payments").to_dict() == {
            "rate": 5,
            "per": "hour",
            "key": "payments",
        }

    def test_key_omitted_when_none(self):
        assert "key" not in RateLimitPolicy(rate=1).to_dict()


class TestRateLimitPolicyFromIntOrPolicy:
    def test_int_builds_policy_with_defaults(self):
        assert RateLimitPolicy.from_int_or_policy(50) == RateLimitPolicy(rate=50)

    def test_policy_passes_through(self):
        p = RateLimitPolicy(rate=3, per="minute", key="x")
        assert RateLimitPolicy.from_int_or_policy(p) is p

    def test_bool_rejected(self):
        with pytest.raises(TypeError, match="bool"):
            RateLimitPolicy.from_int_or_policy(True)  # type: ignore[arg-type]


class TestConcurrencyPolicyToDict:
    def test_max_concurrent_only(self):
        assert ConcurrencyPolicy(max_concurrent=4).to_dict() == {"max_concurrent": 4}

    def test_key_included_when_set(self):
        assert ConcurrencyPolicy(max_concurrent=2, key="gpu").to_dict() == {
            "max_concurrent": 2,
            "key": "gpu",
        }

    def test_key_omitted_when_none(self):
        assert "key" not in ConcurrencyPolicy(max_concurrent=1).to_dict()


class TestConcurrencyPolicyFromIntOrPolicy:
    def test_int_builds_policy(self):
        assert ConcurrencyPolicy.from_int_or_policy(8) == ConcurrencyPolicy(
            max_concurrent=8
        )

    def test_policy_passes_through(self):
        p = ConcurrencyPolicy(max_concurrent=1, key="k")
        assert ConcurrencyPolicy.from_int_or_policy(p) is p

    def test_bool_rejected(self):
        with pytest.raises(TypeError, match="bool"):
            ConcurrencyPolicy.from_int_or_policy(True)  # type: ignore[arg-type]


class TestRetryPolicyKey:
    def test_key_omitted_from_dict_by_default(self):
        assert "key" not in RetryPolicy.exponential(attempts=3).to_dict()

    def test_key_included_when_set_on_exponential(self):
        d = RetryPolicy.exponential(attempts=3, key="shared-retry").to_dict()
        assert d["key"] == "shared-retry"

    def test_key_included_when_set_on_linear(self):
        p = RetryPolicy.linear(attempts=2, delay=0.5, key="k")
        assert p.key == "k"
        assert p.to_dict()["key"] == "k"

    def test_bool_rejected(self):
        with pytest.raises(TypeError, match="bool"):
            RetryPolicy.from_int_or_policy(True)  # type: ignore[arg-type]


class TestBatchingGeneratorGuardFunction:
    def test_sync_generator_with_batching_raises(self):
        def gen(n: int) -> int:
            yield n

        with pytest.raises(FunctionError, match="generator"):
            RemoteFunction(gen, max_batching_size=10)

    def test_async_generator_with_batching_raises(self):
        async def agen(n: int) -> int:
            yield n

        with pytest.raises(FunctionError, match="generator"):
            RemoteFunction(agen, max_batching_size=10)

    def test_sync_generator_allowed_without_batching(self):
        def gen(n: int) -> int:
            yield n

        RemoteFunction(gen)

    def test_plain_function_with_batching_allowed(self):
        def add(x: int, y: int) -> int:
            return x + y

        RemoteFunction(add, max_batching_size=10)


class TestBatchingGeneratorGuardClass:
    def test_sync_generator_method_with_batching_raises(self):
        class G:
            @method
            def gen(self, n: int) -> int:
                for i in range(n):
                    yield i

        with pytest.raises(ClassError, match="generator"):
            RemoteClass(G, max_batching_size=10)

    def test_async_generator_method_with_batching_raises(self):
        class A:
            @method
            async def agen(self, n: int) -> int:
                for i in range(n):
                    yield i

        with pytest.raises(ClassError, match="generator"):
            RemoteClass(A, max_batching_size=10)

    def test_sync_generator_method_allowed_without_batching(self):
        class G:
            @method
            def gen(self, n: int) -> int:
                for i in range(n):
                    yield i

        RemoteClass(G)

    def test_plain_method_with_batching_allowed(self):
        class Adder:
            @method
            def add(self, x: int, y: int) -> int:
                return x + y

        RemoteClass(Adder, max_batching_size=10)
