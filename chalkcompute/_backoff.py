"""Jittered exponential backoff shared by client-side retry loops."""

from __future__ import annotations

import random


class Backoff:
    """Exponential backoff with the same +/-20% jitter as gRPC.

    The nominal step starts at ``min(initial, maximum)`` and doubles up to
    ``maximum``. Each wait is drawn from 0.8 to 1.2 times that step, preserving
    its expected wait. ``maximum`` caps the nominal step; an individual wait
    can reach 1.2 times that cap before clipping to the remaining deadline.
    """

    def __init__(self, initial: float, maximum: float) -> None:
        self._step = min(initial, maximum)
        self._maximum = maximum

    def next_wait(self, remaining: float | None = None) -> float:
        """Return the next wait, clipped to ``remaining`` seconds when given."""
        wait = random.uniform(0.8 * self._step, 1.2 * self._step)
        self._step = min(self._step * 2.0, self._maximum)
        if remaining is not None:
            wait = min(wait, max(remaining, 0.0))
        return wait
