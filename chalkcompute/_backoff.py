"""Jittered exponential backoff shared by client-side retry loops."""

from __future__ import annotations

import random
from typing import Literal


class Backoff:
    """Exponential backoff with centered or full jitter.

    The nominal step starts at ``min(initial, maximum)`` and grows by
    ``multiplier`` up to ``maximum``. Centered jitter samples between 0.8 and
    1.2 times the step, preserving its mean; full jitter samples from zero
    to the step. ``maximum`` caps the nominal step, so centered waits can
    reach 1.2 times that cap before clipping to the remaining deadline.

    Centered jitter follows gRPC A6's retry backoff policy:
    https://github.com/grpc/proposal/blob/master/A6-client-retries.md#exponential-backoff
    """

    def __init__(
        self,
        initial: float,
        maximum: float,
        *,
        jitter: Literal["centered", "full"] = "centered",
        multiplier: float = 2.0,
    ) -> None:
        self._step = min(initial, maximum)
        self._maximum = maximum
        self._jitter = jitter
        self._multiplier = multiplier

    def next_wait(self, remaining: float | None = None) -> float:
        """Return the next wait, clipped to ``remaining`` seconds when given."""
        low, high = (
            (0.0, self._step)
            if self._jitter == "full"
            else (0.8 * self._step, 1.2 * self._step)
        )
        wait = random.uniform(low, high)
        self._step = min(self._step * self._multiplier, self._maximum)
        if remaining is not None:
            wait = min(wait, max(remaining, 0.0))
        return wait
