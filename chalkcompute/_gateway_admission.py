"""Shared gateway ingress capacity across Envoy replicas."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class GatewayAdmissionPolicy:
    """Global direct-ingress cap = routable replicas × request allowance.

    Execution lifetime requires the native serving lease protocol and retains
    slots after caller cancellation. Queue inputs and logical input bytes use
    the separate per-replica ConcurrencyPolicy.
    """

    max_inflight_requests_per_replica: int
    lifetime: Literal["network", "execution"] = "execution"
    max_scale_from_zero_requests: int | None = None

    def __post_init__(self) -> None:
        for name, maximum in (
            ("max_inflight_requests_per_replica", 1_000_000),
            ("max_scale_from_zero_requests", 1_000),
        ):
            value = getattr(self, name)
            if value is None and name == "max_scale_from_zero_requests":
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if not 1 <= value <= maximum:
                raise ValueError(f"{name} must be between 1 and {maximum}")
        if self.lifetime not in ("network", "execution"):
            raise ValueError("lifetime must be network or execution")

    def to_proto(self):
        from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as pb

        result = pb.GatewayAdmissionPolicy(
            max_inflight_requests_per_replica=self.max_inflight_requests_per_replica,
            lifetime=pb.GatewayAdmissionLifetime.NETWORK
            if self.lifetime == "network"
            else pb.GatewayAdmissionLifetime.EXECUTION,
        )
        if self.max_scale_from_zero_requests is not None:
            result.max_scale_from_zero_requests = self.max_scale_from_zero_requests
        return result


def validate_gateway_admission(
    value: GatewayAdmissionPolicy | None,
) -> GatewayAdmissionPolicy | None:
    if value is not None and not isinstance(value, GatewayAdmissionPolicy):
        raise TypeError("gateway_admission must be a GatewayAdmissionPolicy")
    return value
