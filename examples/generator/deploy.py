"""Deploy generator functions (sync and async) to demonstrate streaming results.

Run this file to deploy both functions:

    uv run python examples/generator/deploy.py
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator

from chalkcompute import Image, function

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


@function(name="count-up")
def count_up(n: int) -> Iterator[int]:
    """Sync generator: yields integers 0..n-1."""
    for i in range(n):
        yield i


@function(name="async-count-up")
async def async_count_up(n: int) -> AsyncIterator[int]:
    """Async generator: yields integers 0..n-1 with simulated async work."""
    import asyncio

    for i in range(n):
        await asyncio.sleep(0.01)
        yield i
