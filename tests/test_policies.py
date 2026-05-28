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
from chalkcompute._scaling_group import ScalingGroup, ScalingGroupError
from chalkcompute._image import Image


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
        # User-visible default is "second"
        assert RateLimitPolicy(rate=10).per == "second"

    def test_to_dict_maps_per_to_proto_enum_name(self):
        # Wire format must use the proto enum name so protojson doesn't silently
        # discard the value as unknown (connect-go uses DiscardUnknown: true)
        assert RateLimitPolicy(rate=10, per="second").to_dict()["per"] == "RATE_LIMIT_PER_SECOND"
        assert RateLimitPolicy(rate=10, per="minute").to_dict()["per"] == "RATE_LIMIT_PER_MINUTE"
        assert RateLimitPolicy(rate=10, per="hour").to_dict()["per"] == "RATE_LIMIT_PER_HOUR"

    def test_key_included_when_set(self):
        assert RateLimitPolicy(rate=5, per="hour", key="payments").to_dict() == {
            "rate": 5,
            "per": "RATE_LIMIT_PER_HOUR",
            "key": "payments",
        }

    def test_key_omitted_when_none(self):
        assert "key" not in RateLimitPolicy(rate=1).to_dict()


class TestRateLimitPolicyValidation:
    def test_rate_zero_rejected(self):
        with pytest.raises(ValueError, match="rate must be > 0"):
            RateLimitPolicy(rate=0)

    def test_rate_negative_rejected(self):
        with pytest.raises(ValueError, match="rate must be > 0"):
            RateLimitPolicy(rate=-1)

    def test_invalid_per_rejected(self):
        with pytest.raises(ValueError, match="per must be one of"):
            RateLimitPolicy(rate=10, per="day")

    def test_per_wrong_case_rejected(self):
        with pytest.raises(ValueError, match="per must be one of"):
            RateLimitPolicy(rate=10, per="SECOND")

    def test_empty_per_rejected(self):
        with pytest.raises(ValueError, match="per must be one of"):
            RateLimitPolicy(rate=10, per="")

    def test_valid_per_values_accepted(self):
        for unit in ("second", "minute", "hour"):
            RateLimitPolicy(rate=1, per=unit)


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


class _MethodCls:
    @method
    def run(self, x: int) -> int:
        return x


class TestRemoteClassReplicaValidation:
    def test_min_greater_than_max_rejected(self):
        with pytest.raises(ValueError, match="min_replicas.*must be.*max_replicas"):
            RemoteClass(_MethodCls, min_replicas=5, max_replicas=2)

    def test_max_zero_with_min_one_rejected(self):
        with pytest.raises(ValueError, match="min_replicas.*must be.*max_replicas"):
            RemoteClass(_MethodCls, min_replicas=1, max_replicas=0)

    def test_negative_min_rejected(self):
        with pytest.raises(ValueError, match="min_replicas must be >= 0"):
            RemoteClass(_MethodCls, min_replicas=-1, max_replicas=1)

    def test_negative_max_rejected(self):
        with pytest.raises(ValueError, match="max_replicas must be >= 0"):
            RemoteClass(_MethodCls, min_replicas=0, max_replicas=-1)

    def test_equal_min_max_accepted(self):
        RemoteClass(_MethodCls, min_replicas=2, max_replicas=2)

    def test_min_less_than_max_accepted(self):
        RemoteClass(_MethodCls, min_replicas=1, max_replicas=4)


class TestScalingGroupPortValidation:
    def test_negative_port_rejected(self):
        with pytest.raises(ValueError, match="port must be in range"):
            ScalingGroup(image=Image.debian_slim(), port=-1)

    def test_zero_port_rejected(self):
        with pytest.raises(ValueError, match="port must be in range"):
            ScalingGroup(image=Image.debian_slim(), port=0)

    def test_port_above_max_rejected(self):
        with pytest.raises(ValueError, match="port must be in range"):
            ScalingGroup(image=Image.debian_slim(), port=65536)

    def test_valid_port_accepted(self):
        ScalingGroup(image=Image.debian_slim(), port=8080)
        ScalingGroup(image=Image.debian_slim(), port=1)
        ScalingGroup(image=Image.debian_slim(), port=65535)
