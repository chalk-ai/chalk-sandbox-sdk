"""Tests for policy dataclasses (rate limit / concurrency / retry) and the
deploy-time guards that reject generator handlers + max_batching_size.

The ``to_dict()`` output of each policy is the wire contract — the dict
ends up in ``function_config`` on CreateExternalFunctionVersion, so these
tests lock in the key names the server reads.
"""

from __future__ import annotations

from typing import Any

import pytest

import chalkcompute._cls as cls_mod
import chalkcompute._function as function_mod
from chalkcompute._cls import ClassError, RemoteClass, method
from chalkcompute._concurrency import ConcurrencyPolicy
from chalkcompute._function import FunctionError, RemoteFunction
from chalkcompute._rate_limit import RateLimitPolicy
from chalkcompute._retry import RetryPolicy


class _FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def json(self) -> dict[str, Any]:
        return self._data


class _CapturingClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def rpc(
        self,
        _service: str,
        method: str,
        request: dict[str, Any],
        **_: Any,
    ) -> _FakeResponse:
        if method != "CreateExternalFunctionVersion":
            raise AssertionError(f"unexpected method: {method}")
        self.requests.append(request)
        return _FakeResponse(
            {
                "externalFunctionVersion": {
                    "id": f"version-{len(self.requests)}",
                    "functionName": request["functionName"],
                    "version": len(self.requests),
                    "scalingGroupName": request["spec"]["containerSpec"]["name"],
                    "inputArrowSchema": request["inputArrowSchema"],
                },
                "scalingGroup": {
                    "status": "Running",
                    "webUrl": "http://127.0.0.1",
                    "port": 6666,
                },
            }
        )


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


class TestTracingPolicy:
    def test_none_omits_remote_tracing_config(self):
        def add(x: int) -> int:
            return x + 1

        assert RemoteFunction(add, tracing=None)._tracing_config is None

    def test_sample_rate_builds_policy(self):
        def add(x: int) -> int:
            return x + 1

        assert RemoteFunction(add, tracing=0.1)._tracing_config == {
            "mode": "TRACING_MODE_SAMPLE",
            "sampleRate": 0.1,
        }

    def test_off_builds_hard_suppression_policy(self):
        def add(x: int) -> int:
            return x + 1

        assert RemoteFunction(add, tracing="off")._tracing_config == {
            "mode": "TRACING_MODE_OFF"
        }

    def test_bool_rejected(self):
        def add(x: int) -> int:
            return x + 1

        with pytest.raises(TypeError, match="tracing"):
            RemoteFunction(add, tracing=True)  # type: ignore[arg-type]

    def test_out_of_range_sample_rate_rejected(self):
        def add(x: int) -> int:
            return x + 1

        with pytest.raises(ValueError, match="sample rate"):
            RemoteFunction(add, tracing=1.1)


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


class TestScheduleValidationFunction:
    def test_empty_schedule_rejected(self):
        def tick() -> None:
            pass

        with pytest.raises(ValueError, match="non-empty"):
            RemoteFunction(tick, schedule=" ")

    def test_non_string_schedule_rejected(self):
        def tick() -> None:
            pass

        with pytest.raises(TypeError, match="string"):
            RemoteFunction(tick, schedule=1)  # type: ignore[arg-type]

    def test_inputs_rejected_for_scheduled_function(self):
        def add(x: int) -> int:
            return x

        with pytest.raises(FunctionError, match="cannot have inputs"):
            RemoteFunction(add, schedule="1h")

    def test_schedule_is_sent_in_function_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        def tick() -> None:
            pass

        client = _CapturingClient()
        fn = RemoteFunction(tick, schedule=" 1h ")
        fn._client = client  # type: ignore[assignment]
        monkeypatch.setattr(RemoteFunction, "_build_deploy_image", lambda _: object())
        monkeypatch.setattr(
            function_mod,
            "_build_and_upload",
            lambda *_, **__: ("image:test", []),
        )

        fn.deploy()

        assert client.requests[0]["config"]["schedule"] == "1h"


class TestScheduleValidationClass:
    def test_empty_schedule_rejected(self):
        class Jobs:
            @method
            def tick(self) -> None:
                pass

        with pytest.raises(ValueError, match="non-empty"):
            RemoteClass(Jobs, schedule="")

    def test_non_string_schedule_rejected(self):
        class Jobs:
            @method
            def tick(self) -> None:
                pass

        with pytest.raises(TypeError, match="string"):
            RemoteClass(Jobs, schedule=1)  # type: ignore[arg-type]

    def test_inputs_rejected_for_scheduled_method(self):
        class Jobs:
            @method
            def tick(self, x: int) -> int:
                return x

        with pytest.raises(ClassError, match="cannot have inputs"):
            RemoteClass(Jobs, schedule="1h")

    def test_schedule_is_sent_in_each_method_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        class Jobs:
            @method
            def hourly(self) -> None:
                pass

            @method
            def daily(self) -> None:
                pass

        client = _CapturingClient()
        rc = RemoteClass(Jobs, schedule="0 * * * *")
        rc._client = client  # type: ignore[assignment]
        monkeypatch.setattr(
            RemoteClass, "_build_deploy_image", lambda _: (object(), [])
        )
        monkeypatch.setattr(
            cls_mod,
            "_build_and_upload",
            lambda *_, **__: ("image:test", []),
        )

        rc.deploy()

        assert [req["config"]["schedule"] for req in client.requests] == [
            "0 * * * *",
            "0 * * * *",
        ]
