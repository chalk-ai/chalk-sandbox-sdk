from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

_RemoteFunctionTracingMode = Literal[
    "parentbased_always_off",
    "parentbased_traceidratio",
    "always_off",
]


_MODE_TO_PROTO = {
    "parentbased_always_off": "TRACING_MODE_PARENT_BASED_ALWAYS_OFF",
    "parentbased_traceidratio": "TRACING_MODE_PARENT_BASED_TRACE_ID_RATIO",
    "always_off": "TRACING_MODE_ALWAYS_OFF",
}


@dataclass(frozen=True)
class TracingPolicy:
    """Tracing policy for customer compute spans."""

    sample_rate: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "sample_rate",
            _validate_sample_rate(self.sample_rate),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": _MODE_TO_PROTO["parentbased_traceidratio"],
            "sampleRate": self.sample_rate,
        }


@dataclass(frozen=True)
class _RemoteFunctionTracingPolicy:
    mode: _RemoteFunctionTracingMode
    sample_rate: float | None = None

    def __post_init__(self) -> None:
        if self.mode not in _MODE_TO_PROTO:
            raise ValueError(f"unsupported remote function tracing mode {self.mode!r}")

        if self.mode == "parentbased_traceidratio":
            if self.sample_rate is None:
                raise ValueError(
                    "parentbased_traceidratio tracing requires a sample_rate"
                )
            object.__setattr__(
                self,
                "sample_rate",
                _validate_sample_rate(self.sample_rate),
            )
        elif self.sample_rate is not None:
            raise ValueError(f"{self.mode} tracing does not accept a sample_rate")

    @classmethod
    def always_off(cls) -> _RemoteFunctionTracingPolicy:
        return cls(mode="always_off")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"mode": _MODE_TO_PROTO[self.mode]}
        if self.mode == "parentbased_traceidratio":
            d["sampleRate"] = self.sample_rate
        return d


def _validate_sample_rate(sample_rate: float) -> float:
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int | float):
        raise TypeError("sample_rate must be a number between 0.0 and 1.0")
    rate = float(sample_rate)
    if rate < 0.0 or rate > 1.0:
        raise ValueError("sample_rate must be between 0.0 and 1.0")
    return rate


TracingPolicyConfig = bool | TracingPolicy


def normalize_remote_function_tracing(
    tracing: TracingPolicyConfig | None,
) -> TracingPolicy | _RemoteFunctionTracingPolicy | None:
    if tracing is None:
        return TracingPolicy(sample_rate=1.0)
    if isinstance(tracing, bool):
        return (
            TracingPolicy(sample_rate=1.0)
            if tracing
            else _RemoteFunctionTracingPolicy.always_off()
        )
    if isinstance(tracing, TracingPolicy):
        return tracing
    raise TypeError(
        f"tracing must be a bool or TracingPolicy, got {type(tracing).__name__}"
    )


# ---------------------------------------------------------------------------
# Workload (sandbox / container / scaling group) tracing
# ---------------------------------------------------------------------------

OTEL_TRACES_SAMPLER_ENV_VAR = "OTEL_TRACES_SAMPLER"
OTEL_TRACES_SAMPLER_ARG_ENV_VAR = "OTEL_TRACES_SAMPLER_ARG"

# Standard OpenTelemetry sampler names (OTEL_TRACES_SAMPLER) for each mode.
_MODE_TO_OTEL_SAMPLER: dict[str, str] = {
    "parentbased_always_off": "parentbased_always_off",
    "parentbased_traceidratio": "parentbased_traceidratio",
    "always_off": "always_off",
}


def normalize_workload_tracing(
    tracing: TracingPolicyConfig | None,
) -> TracingPolicy | _RemoteFunctionTracingPolicy | None:
    """Normalize ``tracing=`` for sandboxes, containers, and scaling groups.

    Unlike :func:`normalize_remote_function_tracing`, ``None`` stays ``None``:
    the workload keeps the platform's default sampler instead of opting in.
    """
    if tracing is None:
        return None
    return normalize_remote_function_tracing(tracing)


def workload_tracing_env(
    policy: TracingPolicy | _RemoteFunctionTracingPolicy | None,
) -> dict[str, str]:
    """Environment variables that apply ``policy`` inside a workload container.

    The platform injects ``OTEL_EXPORTER_OTLP_ENDPOINT`` and a default
    ``OTEL_TRACES_SAMPLER``/``OTEL_TRACES_SAMPLER_ARG`` into every workload but
    never overwrites keys the spec already sets, so these two standard
    OpenTelemetry variables are the per-workload override.
    """
    if policy is None:
        return {}
    if isinstance(policy, TracingPolicy):
        return {
            OTEL_TRACES_SAMPLER_ENV_VAR: _MODE_TO_OTEL_SAMPLER[
                "parentbased_traceidratio"
            ],
            OTEL_TRACES_SAMPLER_ARG_ENV_VAR: repr(policy.sample_rate),
        }
    env = {OTEL_TRACES_SAMPLER_ENV_VAR: _MODE_TO_OTEL_SAMPLER[policy.mode]}
    if policy.sample_rate is not None:
        env[OTEL_TRACES_SAMPLER_ARG_ENV_VAR] = repr(policy.sample_rate)
    return env


def merge_workload_tracing_env(
    env: Mapping[str, str],
    policy: TracingPolicy | _RemoteFunctionTracingPolicy | None,
) -> dict[str, str]:
    """Return ``env`` with :func:`workload_tracing_env` added.

    Raises ``ValueError`` when ``env`` already sets one of the sampler
    variables: silently letting either side win would hide a misconfiguration,
    so callers must pick ``tracing=`` or the raw variables, not both.
    """
    additions = workload_tracing_env(policy)
    clashes = sorted(key for key in additions if key in env)
    if clashes:
        raise ValueError(
            f"tracing= conflicts with env keys {clashes}; set the sampler through "
            "tracing= or through env, not both"
        )
    return {**env, **additions}
