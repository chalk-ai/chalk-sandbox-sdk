"""Typed protobuf builders shared by function and class deployments."""

from __future__ import annotations

from typing import Any, Mapping

from chalkcompute._admission import ContainerAdmissionPolicy
from chalkcompute._gen.chalk.container.v1 import service_pb as container_pb
from chalkcompute._concurrency import ConcurrencyPolicy
from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import service_pb as catalog_pb
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from chalkcompute._quota import QueuePolicy
from chalkcompute._rate_limit import RateLimitPolicy
from chalkcompute._remote_function_tracing import TracingPolicy
from chalkcompute._retry import RetryPolicy


def scaling_spec(
    *,
    min_replicas: int,
    max_replicas: int,
    shutdown_delay_seconds: int | None,
    window_seconds: int | None,
    target_cpu_utilization_percentage: int | None,
    target_gpu_utilization_percentage: int | None,
    function_name: str | None = None,
    target_queue_depth: int | None = None,
    cron_scaling_trigger: Any | None = None,
    scale_from_zero_request_policy: Any | None = None,
) -> sg_pb.ScalingSpec:
    """Build the shared autoscaling protobuf without an intermediate dict."""
    return sg_pb.ScalingSpec(
        min_replicas=min_replicas,
        max_replicas=max_replicas,
        shutdown_delay_seconds=shutdown_delay_seconds,
        window_seconds=window_seconds,
        target_cpu_utilization_percentage=target_cpu_utilization_percentage,
        gpu_utilization_trigger=(
            None
            if target_gpu_utilization_percentage is None
            else sg_pb.GpuUtilizationScalingTrigger(
                target_utilization_percentage=target_gpu_utilization_percentage
            )
        ),
        function_queue_depth_trigger=(
            None
            if target_queue_depth is None or function_name is None
            else sg_pb.FunctionQueueDepthScalingTrigger(
                function_name=function_name,
                target_queue_depth=target_queue_depth,
            )
        ),
        cron_scaling_trigger=(
            None
            if cron_scaling_trigger is None
            else sg_pb.CronScalingTrigger(
                timezone=cron_scaling_trigger.timezone,
                windows=[
                    sg_pb.CronScalingWindow(
                        start=window.start,
                        end=window.end,
                        desired_replicas=window.desired_replicas,
                    )
                    for window in cron_scaling_trigger.windows
                ],
            )
        ),
        scale_from_zero_request_policy=(
            None
            if scale_from_zero_request_policy is None
            else sg_pb.ScaleFromZeroRequestPolicy[scale_from_zero_request_policy.name]
        ),
    )


def function_config(
    *,
    serialization_format: str,
    options: Mapping[str, str],
    max_buffer_duration: int | None,
    max_batching_size: int | None,
    retries: RetryPolicy | None,
    rate_limit: RateLimitPolicy | None,
    concurrency: ConcurrencyPolicy | None,
    queue_policy: QueuePolicy | None,
    schedule: str | None,
    tracing: Any | None = None,
    admission: ContainerAdmissionPolicy | None = None,
) -> catalog_pb.FunctionConfig:
    """Build the function configuration protobuf directly from SDK policies."""
    serialization = (
        catalog_pb.SerializationFormat.PYARROW
        if serialization_format == "pyarrow"
        else None
    )
    retry_proto = (
        None
        if retries is None
        else catalog_pb.RetryPolicy(
            max_retries=retries.max_retries,
            initial_backoff_ms=round(retries.initial_wait_seconds * 1000),
            backoff_multiplier=retries.backoff_multiplier,
            max_backoff_ms=round(retries.max_wait_seconds * 1000),
            key=retries.key,
        )
    )
    rate_limit_proto = None
    if rate_limit is not None:
        per = {
            "second": catalog_pb.RateLimitPer.SECOND,
            "minute": catalog_pb.RateLimitPer.MINUTE,
            "hour": catalog_pb.RateLimitPer.HOUR,
        }[rate_limit.per]
        rate_limit_proto = catalog_pb.RateLimitPolicy(
            rate=rate_limit.rate,
            per=per,
            key=rate_limit.key,
        )
    tracing_proto = None
    if tracing is not None:
        if isinstance(tracing, TracingPolicy):
            mode = catalog_pb.TracingMode.PARENT_BASED_TRACE_ID_RATIO
        else:
            mode = {
                "parentbased_always_off": catalog_pb.TracingMode.PARENT_BASED_ALWAYS_OFF,
                "parentbased_traceidratio": catalog_pb.TracingMode.PARENT_BASED_TRACE_ID_RATIO,
                "always_off": catalog_pb.TracingMode.ALWAYS_OFF,
            }[tracing.mode]
        tracing_proto = catalog_pb.TracingPolicy(
            mode=mode,
            sample_rate=tracing.sample_rate,
        )
    return catalog_pb.FunctionConfig(
        serialization_format=serialization,
        options=dict(options),
        max_buffer_duration=max_buffer_duration,
        max_batching_size=max_batching_size,
        retry_policy=retry_proto,
        rate_limit=rate_limit_proto,
        concurrency=(
            None
            if concurrency is None
            else catalog_pb.ConcurrencyPolicy(
                max_concurrent=concurrency.max_concurrent,
                key=concurrency.key,
            )
        ),
        queue=(
            None
            if queue_policy is None
            else catalog_pb.QueuePolicy(
                max_items=queue_policy.max_items,
                result_ttl_seconds=queue_policy.result_ttl_seconds,
            )
        ),
        schedule=schedule,
        tracing=tracing_proto,
        admission=(
            None
            if admission is None
            else container_pb.ContainerAdmissionPolicy(
                max_inflight_requests_per_replica=admission.max_inflight_requests_per_replica,
                max_inflight_input_bytes_per_replica=admission.max_inflight_input_bytes_per_replica,
            )
        ),
    )
