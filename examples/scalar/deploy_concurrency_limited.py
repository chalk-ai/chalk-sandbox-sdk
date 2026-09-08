#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute>=2.11.1"]
# ///

"""Deploy a concurrency-limited scalar function for testing.

Concurrency limit: 2 concurrent calls. The catalog consumer will enforce
this via a semaphore, so at most 2 calls execute simultaneously.

The deployment intentionally persists so multiple clients can exercise the
shared concurrency limit.
"""

from __future__ import annotations

import logging
import time

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


@chalkcompute.function(
    name="ex-slow-add",
    concurrency=chalkcompute.ConcurrencyPolicy(max_concurrent=2),
)
def slow_add(a: int, b: int) -> int:
    """Addition with a 1-second sleep to make concurrency limiting observable."""
    time.sleep(1)
    return a + b
