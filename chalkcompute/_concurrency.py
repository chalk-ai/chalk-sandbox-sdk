from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ConcurrencyPolicy:
    """Async queue execution concurrency and per-replica admission budgets.

    ``max_concurrent`` caps async queue executions through the serving-side
    Redis semaphore. It is independent of the per-replica budgets and does not
    limit direct ``.remote()`` calls. Per-replica limits are enforced
    independently by each serving replica; they are not shared by ``key``.
    """

    max_concurrent: int | None = None
    """Maximum concurrent async queue executions. ``None`` omits an explicit
    queue-gate config and preserves the service default. Direct ``.remote()``
    calls are not capped by this field."""

    key: str | None = None
    """Queue gate grouping key. Defaults to the function identity when omitted.
    Only applies when ``max_concurrent`` is set and never shares per-replica
    budgets."""

    max_inflight_requests_per_replica: int | None = None
    """Maximum accepted unfinished requests per replica, including waiting
    work. ``None`` disables the budget."""

    max_inflight_input_bytes_per_replica: int | None = None
    """Maximum retained logical gRPC input bytes per replica. Excludes queue
    inputs, Python expansion, outputs, and transport buffers. ``None`` disables."""

    def __post_init__(self) -> None:
        if self.max_concurrent is not None:
            if isinstance(self.max_concurrent, bool) or not isinstance(
                self.max_concurrent, int
            ):
                raise TypeError(
                    "max_concurrent must be an int, "
                    f"got {type(self.max_concurrent).__name__}"
                )
            if self.max_concurrent <= 0:
                raise ValueError(f"concurrency must be > 0, got {self.max_concurrent}")
        elif self.key is not None:
            raise ValueError("key requires max_concurrent")

        for name in (
            "max_inflight_requests_per_replica",
            "max_inflight_input_bytes_per_replica",
        ):
            value = getattr(self, name)
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise TypeError(f"{name} must be an integer")
                if not 0 < value <= 2**63 - 1:
                    raise ValueError(f"{name} must be a positive signed 64-bit integer")

    @classmethod
    def from_int_or_policy(cls, value: int | ConcurrencyPolicy) -> ConcurrencyPolicy:
        if isinstance(value, bool):
            raise TypeError(f"Expected int or {cls.__name__}, got bool")
        if isinstance(value, int):
            return cls(max_concurrent=value)
        if isinstance(value, cls):
            return value
        raise TypeError(f"Expected int or {cls.__name__}, got {type(value).__name__}")


def resolve_concurrency_policy(
    concurrency: int | ConcurrencyPolicy | None,
    max_inflight_requests_per_replica: int | None,
    max_inflight_input_bytes_per_replica: int | None,
) -> ConcurrencyPolicy | None:
    """Normalize the combined policy and its per-replica shorthand fields."""
    policy = (
        ConcurrencyPolicy.from_int_or_policy(concurrency)
        if concurrency is not None
        else ConcurrencyPolicy()
    )
    for name, shorthand in (
        ("max_inflight_requests_per_replica", max_inflight_requests_per_replica),
        ("max_inflight_input_bytes_per_replica", max_inflight_input_bytes_per_replica),
    ):
        configured = getattr(policy, name)
        if configured is not None and shorthand is not None:
            raise ValueError(
                f"Specify {name} in concurrency or as a shorthand, not both"
            )
        if shorthand is not None:
            policy = ConcurrencyPolicy(
                max_concurrent=policy.max_concurrent,
                key=policy.key,
                max_inflight_requests_per_replica=(
                    shorthand
                    if name == "max_inflight_requests_per_replica"
                    else policy.max_inflight_requests_per_replica
                ),
                max_inflight_input_bytes_per_replica=(
                    shorthand
                    if name == "max_inflight_input_bytes_per_replica"
                    else policy.max_inflight_input_bytes_per_replica
                ),
            )
    if (
        policy.max_concurrent is None
        and policy.max_inflight_requests_per_replica is None
        and policy.max_inflight_input_bytes_per_replica is None
    ):
        return None
    return policy
