"""Queue quota policy for remote function invocations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_ITEMS = 500_000
"""Default maximum pending items per function queue. Calls beyond this raise
``RESOURCE_EXHAUSTED`` at Submit time. The cluster-level Redis
``maxmemory-policy noeviction`` is a separate backstop for total memory
pressure."""


@dataclass(frozen=True)
class QueuePolicy:
    """Quota for a per-function Redis queue.

    Caps the number of pending items that may be enqueued at once. Enforced
    synchronously at Submit time by the function-queue server: an attempt to
    enqueue past ``max_items`` raises a gRPC ``RESOURCE_EXHAUSTED`` error.

    Examples
    --------
    >>> QueuePolicy(max_items=100_000)
    """

    max_items: int = DEFAULT_MAX_ITEMS
    """Maximum number of pending items in the queue."""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for metadata / JSON export.

        Returns
        -------
        dict[str, Any]
            A JSON-serializable dict representation of the policy.
        """
        return {"max_items": self.max_items}

    @classmethod
    def from_int_or_policy(cls, value: int | QueuePolicy) -> QueuePolicy:
        """Normalize ``int | QueuePolicy`` into a ``QueuePolicy``.

        An ``int`` is treated as ``QueuePolicy(max_items=value)``.

        Parameters
        ----------
        value
            Either a max-items integer shorthand or a pre-built policy.

        Returns
        -------
        QueuePolicy
            The normalized policy.
        """
        if isinstance(value, bool):
            raise TypeError(f"Expected int or {cls.__name__}, got bool")
        if isinstance(value, int):
            return cls(max_items=value)
        return value
