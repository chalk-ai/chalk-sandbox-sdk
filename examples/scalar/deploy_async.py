"""Deploy an async scalar function for testing.

    CHALK_FUNCTION_STRIP_SOURCE=1 uv run python examples/scalar/deploy_async.py
"""

from __future__ import annotations

import logging

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


@chalkcompute.function(name="chalk-sandbox-test-async-add")
async def async_add(a: int, b: int) -> int:
    """Simple async addition."""
    return a + b
