"""Deploy an async scalar function for testing.

uv run python examples/scalar/deploy_async.py
"""

from __future__ import annotations

import logging

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


@chalkcompute.function(name="ex-async-add")
async def async_add(a: int, b: int) -> int:
    """Simple async addition."""
    return a + b
