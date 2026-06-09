"""Deploy a concurrency-limited scalar function for testing.

Concurrency limit: 2 concurrent calls. The catalog consumer will enforce
this via a semaphore, so at most 2 calls execute simultaneously.

    uv run python examples/scalar/deploy_concurrency_limited.py
"""

from __future__ import annotations

import logging
import time

import chalkcompute
from chalkcompute._concurrency import ConcurrencyPolicy

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


@chalkcompute.function(
    name="ex-slow-add",
    concurrency=ConcurrencyPolicy(max_concurrent=2),
)
def slow_add(a: int, b: int) -> int:
    """Addition with a 1-second sleep to make concurrency limiting observable."""
    time.sleep(1)
    return a + b
