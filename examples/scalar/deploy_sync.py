"""Deploy a sync scalar function for testing.

    CHALK_FUNCTION_STRIP_SOURCE=1 uv run python examples/scalar/deploy_sync.py
"""

from __future__ import annotations

import logging

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


@chalkcompute.function(name="chalk-sandbox-test-add")
def add(a: int, b: int) -> int:
    """Simple addition."""
    return a + b
