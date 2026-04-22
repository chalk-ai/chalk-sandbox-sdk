"""Deploy the async generator function.

    uv run python examples/generator/deploy_async.py
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import chalkcompute

# Add examples dir to path so we can import test_utils
sys.path.insert(0, str(Path(__file__).parent.parent))
from test_utils import make_function_name

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


@chalkcompute.function(name=make_function_name("async-count-up"))
async def async_count_up(n: int) -> AsyncIterator[int]:
    """Async generator: yields integers 0..n-1 with simulated async work."""
    import asyncio

    for i in range(n):
        await asyncio.sleep(0.01)
        yield i
