#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute>=2.11.1"]
# ///

"""Deploy a rate-limited scalar function for testing.

Rate limit: 5 requests per second. The catalog consumer will enforce this,
so calls beyond 5/s will be queued and throttled.

The deployment intentionally persists so multiple clients can exercise the
shared rate limit.
"""

from __future__ import annotations

import logging

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


@chalkcompute.function(
    name="ex-add-rate-limited",
    rate_limit=chalkcompute.RateLimitPolicy(rate=5, per="second"),
)
def add_rate_limited(a: int, b: int) -> int:
    """Simple addition with a 5 req/s rate limit."""
    return a + b
