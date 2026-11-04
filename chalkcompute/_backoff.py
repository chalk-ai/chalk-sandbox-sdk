"""Jittered exponential backoff shared by client-side retry loops."""

from __future__ import annotations

import random
from typing import Literal


class Backoff:
    """Exponential backoff with a selectable jitter distribution.

    The nominal step starts at ``min(initial, maximum)`` and grows by
    ``multiplier`` up to ``maximum``. Full jitter draws waits from zero to
    that step. Mean-preserving jitter draws from 0.5 to 1.5 times the step,
    keeping its expected wait for loops bounded by a deadline. In that mode
    an individual wait can exceed ``maximum``, which caps the nominal step.
    """

    def __init__(
        self,
        initial: float,
        maximum: float,
        *,
        multiplier: float = 2.0,
        jitter: Literal["full", "mean_preserving"] = "full",
    ) -> None:
        self._step = min(initial, maximum)
        self._maximum = maximum
        self._multiplier = multiplier
        self._jitter = jitter

    def next_wait(self, remaining: float | None = None) -> float:
        """Return the next wait, clipped to ``remaining`` seconds when given."""
        if self._jitter == "mean_preserving":
            wait = random.uniform(0.5 * self._step, 1.5 * self._step)
        else:
            wait = random.uniform(0.0, self._step)
        self._step = min(self._step * self._multiplier, self._maximum)
        if remaining is not None:
            wait = min(wait, max(remaining, 0.0))
        return wait
