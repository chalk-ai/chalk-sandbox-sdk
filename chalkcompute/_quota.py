"""Queue policy for remote function invocations."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_ITEMS = 500_000
"""Default maximum pending items per function queue. Calls beyond this raise
``RESOURCE_EXHAUSTED`` at Submit time. The cluster-level Redis
``maxmemory-policy noeviction`` is a separate backstop for total memory
pressure."""

DEFAULT_RESULT_TTL_SECONDS = 86_400
"""Default retention for asynchronous call result streams in Redis."""


@dataclass(frozen=True)
class QueuePolicy:
    """Configuration for a per-function Redis queue.

    Caps the number of pending items that may be enqueued at once and controls
    how long asynchronous call result streams remain available. The queue server
    rejects an enqueue past ``max_items`` with ``RESOURCE_EXHAUSTED``.

    Examples
    --------
    >>> QueuePolicy(max_items=100_000, result_ttl_seconds=3_600)
    """

    max_items: int = DEFAULT_MAX_ITEMS
    """Maximum number of pending items in the queue."""

    result_ttl_seconds: int = DEFAULT_RESULT_TTL_SECONDS
    """Retention after completion for asynchronous result streams."""

    def __post_init__(self) -> None:
        if isinstance(self.result_ttl_seconds, bool) or not isinstance(
            self.result_ttl_seconds, int
        ):
            raise TypeError(
                "result_ttl_seconds must be an int, "
                f"got {type(self.result_ttl_seconds).__name__}"
            )
        if self.result_ttl_seconds <= 0:
            raise ValueError(
                f"result_ttl_seconds must be > 0, got {self.result_ttl_seconds}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for metadata / JSON export.

        Returns
        -------
        dict[str, Any]
            A JSON-serializable dict representation of the policy.
        """
        return {
            "max_items": self.max_items,
            "result_ttl_seconds": self.result_ttl_seconds,
        }

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


def normalize_queue_policy(
    *,
    queue: QueuePolicy | None,
    quota: int | QueuePolicy | None,
) -> QueuePolicy | None:
    """Resolve the queue policy while supporting the legacy quota argument."""
    if queue is not None and quota is not None:
        raise ValueError("queue and quota cannot both be specified")
    if queue is not None:
        if not isinstance(queue, QueuePolicy):
            raise TypeError(f"queue must be a QueuePolicy, got {type(queue).__name__}")
        return queue
    if quota is None:
        return None
    warnings.warn(
        "quota= is deprecated; use queue=QueuePolicy(max_items=...) instead",
        DeprecationWarning,
        stacklevel=3,
    )
    return QueuePolicy.from_int_or_policy(quota)
