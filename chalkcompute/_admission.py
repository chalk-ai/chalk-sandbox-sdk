from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContainerAdmissionPolicy:
    """Hard admission budgets enforced independently by each serving replica.

    Request capacity covers accepted work waiting for execution, running work,
    and response retention. Caller cancellation does not release running work.
    Input bytes count logical gRPC payloads, excluding queue inputs, Python
    expansion, outputs, and transport buffers. Neither budget limits QPS.
    ``None`` disables the corresponding budget.
    """

    max_inflight_requests_per_replica: int | None = None
    max_inflight_input_bytes_per_replica: int | None = None

    def __post_init__(self) -> None:
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


def resolve_admission_policy(
    admission: ContainerAdmissionPolicy | None,
    max_inflight_requests_per_replica: int | None,
    max_inflight_input_bytes_per_replica: int | None,
) -> ContainerAdmissionPolicy | None:
    """Normalize the policy and shorthand without ambiguous precedence."""
    if admission is not None:
        if not isinstance(admission, ContainerAdmissionPolicy):
            raise TypeError("admission must be a ContainerAdmissionPolicy")
        if (
            max_inflight_requests_per_replica is not None
            or max_inflight_input_bytes_per_replica is not None
        ):
            raise ValueError(
                "Specify admission or the per-replica admission parameters, not both"
            )
        return admission
    if (
        max_inflight_requests_per_replica is None
        and max_inflight_input_bytes_per_replica is None
    ):
        return None
    return ContainerAdmissionPolicy(
        max_inflight_requests_per_replica=max_inflight_requests_per_replica,
        max_inflight_input_bytes_per_replica=max_inflight_input_bytes_per_replica,
    )
