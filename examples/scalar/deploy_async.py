#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute>=2.11.1"]
# ///

"""Deploy an async scalar function for testing.

The deployment intentionally persists for subsequent remote calls.
"""

from __future__ import annotations

import logging

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


@chalkcompute.function(name="ex-async-add")
async def async_add(a: int, b: int) -> int:
    """Simple async addition."""
    return a + b
