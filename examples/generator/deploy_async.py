#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute>=2.11.1"]
# ///

"""Deploy the async generator function.

The deployment intentionally persists so clients can stream and inspect its
yielded values independently of the deploy process.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


@chalkcompute.function(
    name=os.environ.get("CHALK_E2E_FUNCTION_NAME", "ex-async-count-up")
)
async def async_count_up(n: int) -> AsyncIterator[int]:
    """Async generator: yields integers 0..n-1 with simulated async work."""
    import asyncio

    for i in range(n):
        await asyncio.sleep(0.01)
        yield i
