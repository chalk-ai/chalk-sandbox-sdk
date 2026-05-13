from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"rate": self.rate, "per": self.per}
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
