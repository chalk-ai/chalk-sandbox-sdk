"""Deploy the async generator function.

uv run python examples/generator/deploy_async.py
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


@chalkcompute.function(name="ex-async-count-up")
async def async_count_up(n: int) -> AsyncIterator[int]:
    """Async generator: yields integers 0..n-1 with simulated async work."""
    import asyncio

    for i in range(n):
        await asyncio.sleep(0.01)
        yield i
