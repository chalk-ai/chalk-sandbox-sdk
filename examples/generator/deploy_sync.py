"""Deploy the sync generator function.

uv run python examples/generator/deploy_sync.py
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


@chalkcompute.function(name="chalk-sandbox-test-count-up")
def count_up(n: int) -> Iterator[int]:
    """Sync generator: yields integers 0..n-1."""
    for i in range(n):
        yield i
