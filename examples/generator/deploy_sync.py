#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute>=2.11.1"]
# ///

"""Deploy the sync generator function.

The deployment intentionally persists so clients can stream and inspect its
yielded values independently of the deploy process.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


@chalkcompute.function(name=os.environ.get("CHALK_E2E_FUNCTION_NAME", "ex-count-up"))
def count_up(n: int) -> Iterator[int]:
    """Sync generator: yields integers 0..n-1."""
    for i in range(n):
        yield i
