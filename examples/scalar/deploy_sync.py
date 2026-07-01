"""Deploy a sync scalar function for testing.

uv run python examples/scalar/deploy_sync.py
"""

from __future__ import annotations

import logging

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


@chalkcompute.function(name="add-avi")
def add(a: int, b: int) -> int:
    """Simple addition."""
    return a + b
