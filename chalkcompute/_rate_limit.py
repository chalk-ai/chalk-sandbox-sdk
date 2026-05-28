from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_VALID_PER_UNITS = frozenset({"second", "minute", "hour"})

_PER_TO_PROTO = {
    "second": "RATE_LIMIT_PER_SECOND",
    "minute": "RATE_LIMIT_PER_MINUTE",
    "hour": "RATE_LIMIT_PER_HOUR",
}


@dataclass
class RateLimitPolicy:
    """Rate limit policy for a remote function.

    Caps the outbound request rate across all callers sharing the same
    ``key``. Enforced before each outbound RPC.
    """

    rate: int
    """Maximum requests per ``per`` unit."""

    per: str = "second"
    """Time unit: ``"second"``, ``"minute"``, or ``"hour"``."""

    key: str | None = None
    """Shared-bucket identifier. Functions using the same key share one
    limiter. Defaults to the function's qualified name if omitted."""

    def __post_init__(self) -> None:
        if self.rate <= 0:
            raise ValueError(f"rate must be > 0, got {self.rate}")
        if self.per not in _VALID_PER_UNITS:
            raise ValueError(
                f"per must be one of {sorted(_VALID_PER_UNITS)!r}, got {self.per!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"rate": self.rate, "per": _PER_TO_PROTO[self.per]}
        if self.key is not None:
            d["key"] = self.key
        return d

    @classmethod
    def from_int_or_policy(cls, value: int | RateLimitPolicy) -> RateLimitPolicy:
        if isinstance(value, bool):
            raise TypeError(f"Expected int or {cls.__name__}, got bool")
        if isinstance(value, int):
            return cls(rate=value)
        return value
