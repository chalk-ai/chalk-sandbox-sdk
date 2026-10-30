"""Tests for remote function policies and their deploy-time guards."""

from __future__ import annotations

from typing import Any

import pytest
from protobuf import Oneof

from chalkcompute._cls import ClassError, RemoteClass, method
from chalkcompute._concurrency import ConcurrencyPolicy
from chalkcompute._deployment_proto import function_config
from chalkcompute._function import FunctionError, RemoteFunction
from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import service_pb as catalog_pb
from chalkcompute._quota import DEFAULT_RESULT_TTL_SECONDS, QueuePolicy
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from chalkcompute._rate_limit import RateLimitPolicy
from chalkcompute._remote_function_tracing import (
    TracingPolicy,
    merge_workload_tracing_env,
    normalize_remote_function_tracing,
    normalize_workload_tracing,
    workload_tracing_env,
)
from chalkcompute._retry import RetryPolicy
from chalkcompute._scaling_group import ScalingGroup
from chalkcompute._image import Image
from tests._fakes import FakeMethod, StubbedConnectClient


class _CapturingClient(StubbedConnectClient):
    def __init__(self) -> None:
        self.requests: list[catalog_pb.ExternalFunctionVersionSpec] = []
        self._env_id: str | None = None

    def _ensure_auth(self) -> None:
        pass

    def call(
        self,
        method: FakeMethod,
        request: Any,
        **_: Any,
    ) -> Any:
        if method.name == "get_scaling_group":
            return sg_pb.GetScalingGroupResponse(
                scaling_group=sg_pb.ScalingGroupResponse(
                    status="Running",
                    web_url="http://127.0.0.1",
                    ready_replicas=1,
                    available_replicas=1,
                )
            )
        if method.name != "ensure_external_function":
            raise AssertionError(f"unexpected method: {method.name}")
        assert isinstance(request, catalog_pb.EnsureExternalFunctionRequest)
        assert request.spec is not None and request.spec.version_spec is not None
        assert request.spec.version_spec.spec is not None
        assert request.spec.version_spec.spec.container_spec is not None
        self.requests.append(request.spec.version_spec)
        return catalog_pb.EnsureExternalFunctionResponse(
            result=Oneof(
                "complete",
                catalog_pb.EnsuredExternalFunction(
                    external_function=catalog_pb.ExternalFunction(
                        id=f"version-{len(self.requests)}",
                        name=request.function_name,
                        current_version=catalog_pb.ExternalFunctionVersion(
                            id=f"version-{len(self.requests)}",
                            function_id="function-1",
                            function_name=request.function_name,
                            version=len(self.requests),
                            scaling_group_name=request.spec.version_spec.spec.container_spec.name,
                            input_arrow_schema=request.spec.version_spec.input_arrow_schema,
                        ),
                    ),
                ),
            )
        )


class TestRateLimitPolicyToDict:
    def test_rate_only_defaults_per_to_second(self):
        # User-visible default is "second"
        assert RateLimitPolicy(rate=10).per == "second"

    def test_to_dict_maps_per_to_proto_enum_name(self):
        # Wire format must use the proto enum name so protojson doesn't silently
        # discard the value as unknown (connect-go uses DiscardUnknown: true)
        assert (
            RateLimitPolicy(rate=10, per="second").to_dict()["per"]
            == "RATE_LIMIT_PER_SECOND"
        )
        assert (
            RateLimitPolicy(rate=10, per="minute").to_dict()["per"]
            == "RATE_LIMIT_PER_MINUTE"
        )
        assert (
            RateLimitPolicy(rate=10, per="hour").to_dict()["per"]
            == "RATE_LIMIT_PER_HOUR"
        )

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


class TestConcurrencyPolicyValidation:
    def test_max_concurrent_zero_rejected(self):
        with pytest.raises(ValueError, match="concurrency must be > 0"):
            ConcurrencyPolicy(max_concurrent=0)

    def test_max_concurrent_negative_rejected(self):
        with pytest.raises(ValueError, match="concurrency must be > 0"):
            ConcurrencyPolicy(max_concurrent=-1)

    def test_max_concurrent_bool_rejected(self):
        with pytest.raises(TypeError, match="max_concurrent must be an int"):
            ConcurrencyPolicy(max_concurrent=True)  # type: ignore[arg-type]


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


class TestQueuePolicy:
    def test_result_ttl_defaults_to_one_day(self):
        assert QueuePolicy().result_ttl_seconds == DEFAULT_RESULT_TTL_SECONDS

    def test_result_ttl_is_included_in_metadata(self):
        assert QueuePolicy(result_ttl_seconds=3_600).to_dict() == {
            "max_items": 500_000,
            "result_ttl_seconds": 3_600,
        }

    @pytest.mark.parametrize("ttl", [0, -1])
    def test_non_positive_result_ttl_is_rejected(self, ttl: int):
        with pytest.raises(ValueError, match="result_ttl_seconds must be > 0"):
            QueuePolicy(result_ttl_seconds=ttl)

    def test_non_integer_result_ttl_is_rejected(self):
        with pytest.raises(TypeError, match="result_ttl_seconds must be an int"):
            QueuePolicy(result_ttl_seconds=True)  # type: ignore[arg-type]

    def test_result_ttl_is_sent_in_function_config(self):
        config = function_config(
            serialization_format="pyarrow",
            options={},
            max_buffer_duration=None,
            max_batching_size=None,
            retries=None,
            rate_limit=None,
            concurrency=None,
            queue_policy=QueuePolicy(result_ttl_seconds=3_600),
            schedule=None,
        )

        assert config.queue is not None
        assert config.queue.result_ttl_seconds == 3_600

    def test_remote_function_accepts_queue_policy(self):
        def add(x: int) -> int:
            return x + 1

        policy = QueuePolicy(result_ttl_seconds=3_600)
        remote_function = RemoteFunction(add, queue_policy=policy)

        assert remote_function._queue_policy is policy

    def test_remote_class_accepts_queue_policy(self):
        class Worker:
            @method
            def run(self) -> int:
                return 1

        policy = QueuePolicy(result_ttl_seconds=3_600)
        remote_class = RemoteClass(Worker, queue_policy=policy)

        assert remote_class._queue_policy is policy

    def test_queue_policy_rejects_other_types(self):
        def add(x: int) -> int:
            return x + 1

        with pytest.raises(TypeError, match="queue_policy must be a QueuePolicy"):
            RemoteFunction(add, queue_policy=10)  # type: ignore[arg-type]


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

    def test_negative_retries_rejected(self):
        with pytest.raises(ValueError, match="max_retries must be >= 0"):
            RetryPolicy.exponential(attempts=-1)

    def test_non_integer_retries_rejected(self):
        with pytest.raises(TypeError, match="max_retries must be an int"):
            RetryPolicy(max_retries=1.5)  # type: ignore[arg-type]


class TestTracingPolicy:
    def test_sample_rate_to_dict(self):
        assert TracingPolicy(sample_rate=0.25).to_dict() == {
            "mode": "TRACING_MODE_PARENT_BASED_TRACE_ID_RATIO",
            "sampleRate": 0.25,
        }

    def test_sample_rate_validation(self):
        with pytest.raises(ValueError, match="between 0.0 and 1.0"):
            TracingPolicy(sample_rate=1.5)

        with pytest.raises(TypeError, match="number"):
            TracingPolicy(sample_rate=True)  # type: ignore[arg-type]

    def test_bool_tracing_config_normalization(self):
        assert normalize_remote_function_tracing(None) == TracingPolicy(sample_rate=1.0)
        assert normalize_remote_function_tracing(True) == TracingPolicy(sample_rate=1.0)
        assert normalize_remote_function_tracing(False).to_dict() == {
            "mode": "TRACING_MODE_ALWAYS_OFF"
        }

    def test_invalid_tracing_config_rejected(self):
        with pytest.raises(TypeError, match="tracing must be a bool or TracingPolicy"):
            normalize_remote_function_tracing("on")  # type: ignore[arg-type]


class TestWorkloadTracing:
    """``tracing=`` on sandboxes, containers, and scaling groups."""

    def test_none_keeps_platform_default(self):
        assert normalize_workload_tracing(None) is None
        assert workload_tracing_env(None) == {}
        assert merge_workload_tracing_env({"A": "1"}, None) == {"A": "1"}

    def test_true_traces_every_root(self):
        policy = normalize_workload_tracing(True)
        assert policy == TracingPolicy(sample_rate=1.0)
        assert workload_tracing_env(policy) == {
            "OTEL_TRACES_SAMPLER": "parentbased_traceidratio",
            "OTEL_TRACES_SAMPLER_ARG": "1.0",
        }

    def test_policy_sets_root_sample_rate(self):
        assert workload_tracing_env(TracingPolicy(sample_rate=0.25)) == {
            "OTEL_TRACES_SAMPLER": "parentbased_traceidratio",
            "OTEL_TRACES_SAMPLER_ARG": "0.25",
        }

    def test_false_turns_tracing_off(self):
        assert workload_tracing_env(normalize_workload_tracing(False)) == {
            "OTEL_TRACES_SAMPLER": "always_off"
        }

    def test_merge_preserves_user_env(self):
        assert merge_workload_tracing_env(
            {"A": "1"}, TracingPolicy(sample_rate=0.5)
        ) == {
            "A": "1",
            "OTEL_TRACES_SAMPLER": "parentbased_traceidratio",
            "OTEL_TRACES_SAMPLER_ARG": "0.5",
        }

    def test_merge_rejects_conflicting_env(self):
        with pytest.raises(ValueError, match=r"OTEL_TRACES_SAMPLER_ARG"):
            merge_workload_tracing_env(
                {"OTEL_TRACES_SAMPLER_ARG": "0.1"}, TracingPolicy(sample_rate=1.0)
            )

    def test_invalid_config_rejected(self):
        with pytest.raises(TypeError, match="tracing must be a bool or TracingPolicy"):
            normalize_workload_tracing("on")  # type: ignore[arg-type]


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
        monkeypatch.setattr(
            RemoteFunction, "_build_deploy_image", lambda _: Image("image:test")
        )

        fn.deploy()

        assert client.requests[0].config is not None
        assert client.requests[0].config.schedule == "1h"

    def test_tracing_is_sent_in_function_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        def add(x: int, y: int) -> int:
            return x + y

        client = _CapturingClient()
        fn = RemoteFunction(add, tracing=TracingPolicy(sample_rate=0.25))
        fn._client = client  # type: ignore[assignment]
        monkeypatch.setattr(
            RemoteFunction, "_build_deploy_image", lambda _: Image("image:test")
        )

        fn.deploy()

        tracing = (
            client.requests[0].config.tracing if client.requests[0].config else None
        )
        assert tracing is not None
        assert tracing.mode == catalog_pb.TracingMode.PARENT_BASED_TRACE_ID_RATIO
        assert tracing.sample_rate == 0.25

    def test_tracing_true_turns_on_remote_function_tracing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        def add(x: int, y: int) -> int:
            return x + y

        client = _CapturingClient()
        fn = RemoteFunction(add, tracing=True)
        fn._client = client  # type: ignore[assignment]
        monkeypatch.setattr(
            RemoteFunction, "_build_deploy_image", lambda _: Image("image:test")
        )

        fn.deploy()

        tracing = (
            client.requests[0].config.tracing if client.requests[0].config else None
        )
        assert tracing is not None
        assert tracing.mode == catalog_pb.TracingMode.PARENT_BASED_TRACE_ID_RATIO
        assert tracing.sample_rate == 1.0

    def test_tracing_false_turns_off_remote_function_tracing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        def add(x: int, y: int) -> int:
            return x + y

        client = _CapturingClient()
        fn = RemoteFunction(add, tracing=False)
        fn._client = client  # type: ignore[assignment]
        monkeypatch.setattr(
            RemoteFunction, "_build_deploy_image", lambda _: Image("image:test")
        )

        fn.deploy()

        tracing = (
            client.requests[0].config.tracing if client.requests[0].config else None
        )
        assert tracing is not None
        assert tracing.mode == catalog_pb.TracingMode.ALWAYS_OFF

    def test_remote_class_tracing_is_sent_in_function_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        class Worker:
            @method
            def run(self) -> None:
                pass

        client = _CapturingClient()
        remote_class = RemoteClass(Worker, tracing=TracingPolicy(sample_rate=0.25))
        remote_class._client = client  # type: ignore[assignment]
        monkeypatch.setattr(
            RemoteClass, "_build_deploy_image", lambda _: (Image("image:test"), [])
        )

        remote_class.deploy()

        tracing = (
            client.requests[0].config.tracing if client.requests[0].config else None
        )
        assert tracing is not None
        assert tracing.mode == catalog_pb.TracingMode.PARENT_BASED_TRACE_ID_RATIO
        assert tracing.sample_rate == 0.25

    def test_tracing_config_included_by_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        def add(x: int, y: int) -> int:
            return x + y

        client = _CapturingClient()
        fn = RemoteFunction(add)
        fn._client = client  # type: ignore[assignment]
        monkeypatch.setattr(
            RemoteFunction, "_build_deploy_image", lambda _: Image("image:test")
        )

        fn.deploy()

        tracing = (
            client.requests[0].config.tracing if client.requests[0].config else None
        )
        assert tracing is not None
        assert tracing.mode == catalog_pb.TracingMode.PARENT_BASED_TRACE_ID_RATIO
        assert tracing.sample_rate == 1.0


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
            RemoteClass, "_build_deploy_image", lambda _: (Image("image:test"), [])
        )

        rc.deploy()

        assert [
            req.config.schedule if req.config else None for req in client.requests
        ] == [
            "0 * * * *",
            "0 * * * *",
        ]


class TestFunctionQueueProtocolAdvertisement:
    def test_remote_function_advertises_stream_support(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        def run() -> None:
            pass

        client = _CapturingClient()
        fn = RemoteFunction(run)
        fn._client = client  # type: ignore[assignment]
        monkeypatch.setattr(
            RemoteFunction, "_build_deploy_image", lambda _: Image("image:test")
        )

        fn.deploy()

        container_spec = client.requests[0].spec.container_spec
        assert container_spec is not None
        assert container_spec.env_vars["CHALK_FNQ_STREAMS_SUPPORTED"] == "true"
        assert "CHALK_FNQ_QUEUE_PROTOCOL" not in container_spec.env_vars

    def test_user_can_disable_stream_support(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        def run() -> None:
            pass

        client = _CapturingClient()
        fn = RemoteFunction(run, env={"CHALK_FNQ_STREAMS_SUPPORTED": "false"})
        fn._client = client  # type: ignore[assignment]
        monkeypatch.setattr(
            RemoteFunction, "_build_deploy_image", lambda _: Image("image:test")
        )

        fn.deploy()

        container_spec = client.requests[0].spec.container_spec
        assert container_spec is not None
        assert container_spec.env_vars["CHALK_FNQ_STREAMS_SUPPORTED"] == "false"
        assert "CHALK_FNQ_QUEUE_PROTOCOL" not in container_spec.env_vars

    def test_remote_class_advertises_stream_support(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        class Worker:
            @method
            def run(self) -> None:
                pass

        client = _CapturingClient()
        remote_class = RemoteClass(Worker)
        remote_class._client = client  # type: ignore[assignment]
        monkeypatch.setattr(
            RemoteClass, "_build_deploy_image", lambda _: (Image("image:test"), [])
        )

        remote_class.deploy()

        container_spec = client.requests[0].spec.container_spec
        assert container_spec is not None
        assert container_spec.env_vars["CHALK_FNQ_STREAMS_SUPPORTED"] == "true"
        assert "CHALK_FNQ_QUEUE_PROTOCOL" not in container_spec.env_vars


class _MethodCls:
    @method
    def run(self, x: int) -> int:
        return x


def test_remote_function_gpu_trigger_allowed_when_disabled():
    def run(x: int) -> int:
        return x

    RemoteFunction(
        run,
        gpu="nvidia-l4",
        min_replicas=0,
        max_replicas=0,
        target_gpu_utilization_percentage=70,
    )


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

    def test_gpu_trigger_allowed_when_disabled(self):
        RemoteClass(
            _MethodCls,
            gpu="nvidia-l4",
            min_replicas=0,
            max_replicas=0,
            target_gpu_utilization_percentage=70,
        )


class TestScalingGroupPortValidation:
    def test_negative_port_rejected(self):
        with pytest.raises(ValueError, match="port must be between"):
            ScalingGroup(image=Image.debian_slim(), port=-1)

    def test_zero_port_rejected(self):
        with pytest.raises(ValueError, match="port must be between"):
            ScalingGroup(image=Image.debian_slim(), port=0)

    def test_port_above_max_rejected(self):
        with pytest.raises(ValueError, match="port must be between"):
            ScalingGroup(image=Image.debian_slim(), port=65536)

    def test_valid_port_accepted(self):
        ScalingGroup(image=Image.debian_slim(), port=8080)
        ScalingGroup(image=Image.debian_slim(), port=1)
        ScalingGroup(image=Image.debian_slim(), port=65535)


def test_scaling_group_rejects_min_greater_than_max() -> None:
    with pytest.raises(ValueError, match="min_replicas.*must be.*max_replicas"):
        ScalingGroup(image=Image.debian_slim(), min_replicas=5, max_replicas=2)


def test_remote_function_forwards_shared_scaling_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chalkcompute import ReadinessProbe, ScaleFromZeroRequestPolicy

    def add(x: int) -> int:
        return x

    client = _CapturingClient()
    fn = RemoteFunction(
        add,
        name="shared-fields",
        tags={"team": "compute"},
        chalk_identity=True,
        authenticated=True,
        readiness_probe=ReadinessProbe.http("/readyz"),
        scale_from_zero_request_policy=ScaleFromZeroRequestPolicy.HOLD,
    )
    fn._client = client  # type: ignore[assignment]
    monkeypatch.setattr(
        RemoteFunction, "_build_deploy_image", lambda _: Image("image:test")
    )

    fn.deploy()

    spec = client.requests[0].spec
    assert spec is not None
    container_spec = spec.container_spec
    assert container_spec is not None
    assert dict(container_spec.tags) == {"team": "compute"}
    assert container_spec.has_field("chalk_workload_identity")
    assert container_spec.authentication == "AUTHENTICATED"
    assert container_spec.readiness_probe is not None
    assert spec.scaling_spec is not None
    assert (
        spec.scaling_spec.scale_from_zero_request_policy
        == sg_pb.ScaleFromZeroRequestPolicy.HOLD
    )


def test_remote_class_forwards_shared_scaling_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chalkcompute import ReadinessProbe, ScaleFromZeroRequestPolicy

    class Worker:
        @method
        def run(self) -> None:
            pass

    client = _CapturingClient()
    remote_class = RemoteClass(
        Worker,
        name="shared-class",
        tags={"team": "compute"},
        chalk_identity=True,
        authenticated=True,
        readiness_probe=ReadinessProbe.http("/readyz"),
        scale_from_zero_request_policy=ScaleFromZeroRequestPolicy.HOLD,
    )
    remote_class._client = client  # type: ignore[assignment]
    monkeypatch.setattr(
        RemoteClass, "_build_deploy_image", lambda _: (Image("image:test"), [])
    )

    remote_class.deploy()

    spec = client.requests[0].spec
    assert spec is not None
    container_spec = spec.container_spec
    assert container_spec is not None
    assert dict(container_spec.tags) == {"team": "compute"}
    assert container_spec.has_field("chalk_workload_identity")
    assert container_spec.authentication == "AUTHENTICATED"
    assert container_spec.readiness_probe is not None
    assert spec.scaling_spec is not None
    assert (
        spec.scaling_spec.scale_from_zero_request_policy
        == sg_pb.ScaleFromZeroRequestPolicy.HOLD
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("initial_wait_seconds", -1),
        ("initial_wait_seconds", float("nan")),
        ("max_wait_seconds", float("inf")),
        ("backoff_multiplier", 0.5),
        ("backoff_multiplier", float("nan")),
        ("max_wait_seconds", 0.5),
        ("max_retries", 2**31),
    ],
)
def test_retry_policy_rejects_invalid_bounds(field, value):
    with pytest.raises((ValueError, TypeError)):
        RetryPolicy(**{field: value})


@pytest.mark.parametrize(
    "policy",
    [
        RetryPolicy(retry_on=(TimeoutError,)),
        RetryPolicy(key="shared"),
    ],
)
def test_call_options_reject_unimplemented_retry_filters(policy):
    def add(x: int) -> int:
        return x + 1

    with pytest.raises(ValueError, match="not supported"):
        RemoteFunction(add).with_options(retries=policy)


def test_retry_policy_serializes_jitter():
    policy = RetryPolicy.linear(attempts=2, delay=0.25, jitter=False)
    config = function_config(
        serialization_format="pyarrow",
        options={},
        max_buffer_duration=None,
        max_batching_size=None,
        retries=policy,
        rate_limit=None,
        concurrency=None,
        queue_policy=None,
        schedule=None,
    )
    assert config.retry_policy.jitter is False
