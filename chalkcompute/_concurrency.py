from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ConcurrencyPolicy:
    """Concurrency policy for a remote function.

    Caps in-flight handler invocations across all callers sharing the same
    ``key``. Enforced by the dispatcher (chalkdf or the queue consumer)
    before an RPC leaves.
    """

    max_concurrent: int
    """Maximum number of in-flight handler invocations."""

    key: str | None = None
    """Shared-bucket identifier. Functions using the same key share one
    concurrency gate. Defaults to the function's qualified name if omitted."""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"max_concurrent": self.max_concurrent}
        if self.key is not None:
            d["key"] = self.key
        return d

    @classmethod
    def from_int_or_policy(
        cls, value: int | ConcurrencyPolicy
    ) -> ConcurrencyPolicy:
        if isinstance(value, bool):
            raise TypeError(f"Expected int or {cls.__name__}, got bool")
        if isinstance(value, int):
            return cls(max_concurrent=value)
        return value
