#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute>=2.11.1"]
# ///

"""Deploy a function used to exercise deferred-result expiration."""

from __future__ import annotations

import os

import chalkcompute


@chalkcompute.function(
    name=os.environ.get("CHALK_E2E_FUNCTION_NAME", "ex-result-ttl"),
    # Leave time for polling and CI scheduling before testing expiration.
    queue_policy=chalkcompute.QueuePolicy(result_ttl_seconds=15),
)
def echo(token: str) -> str:
    """Return a value whose deferred result will shortly expire."""
    return token
