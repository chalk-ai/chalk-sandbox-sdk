"""Jittered exponential backoff shared by client-side retry loops."""

from __future__ import annotations

import random


class Backoff:
    """Exponential backoff with full jitter.

    Each wait is drawn uniformly between zero and the current step, which
    starts at ``initial`` and grows by ``multiplier`` up to ``maximum``. This
    matches gRPC's retry policy, so callers that fail together spread their
    retries out instead of hitting a recovering server in lockstep.
    """

    def __init__(
        self, initial: float, maximum: float, *, multiplier: float = 2.0
    ) -> None:
        self._step = initial
        self._maximum = maximum
        self._multiplier = multiplier

    def next_wait(self, remaining: float | None = None) -> float:
        """Return the next wait, clipped to ``remaining`` seconds when given."""
        wait = random.uniform(0.0, self._step)
        self._step = min(self._step * self._multiplier, self._maximum)
        if remaining is not None:
            wait = min(wait, max(remaining, 0.0))
        return wait
