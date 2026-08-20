from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ConcurrencyPolicy:
    """Concurrency policy for a remote function.

    Caps in-flight handler invocations across all callers sharing the same
    ``key``. Enforced before each outbound RPC.
    """

    max_concurrent: int
    """Maximum number of in-flight handler invocations."""

    key: str | None = None
    """Shared-bucket identifier. Functions using the same key share one
    concurrency gate. Defaults to the function's qualified name if omitted."""

    def __post_init__(self) -> None:
        if isinstance(self.max_concurrent, bool) or not isinstance(
            self.max_concurrent, int
        ):
            raise TypeError(
                "max_concurrent must be an int, "
                f"got {type(self.max_concurrent).__name__}"
            )
        if self.max_concurrent <= 0:
            raise ValueError(
                f"concurrency must be > 0, got {self.max_concurrent}"
            )

    @classmethod
    def from_int_or_policy(cls, value: int | ConcurrencyPolicy) -> ConcurrencyPolicy:
        if isinstance(value, bool):
            raise TypeError(f"Expected int or {cls.__name__}, got bool")
        if isinstance(value, int):
            return cls(max_concurrent=value)
        return value
