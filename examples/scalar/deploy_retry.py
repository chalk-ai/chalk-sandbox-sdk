#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute>=2.11.1"]
# ///

"""Deploy a function used to exercise queue retries."""

from __future__ import annotations

import os

import chalkcompute

_ATTEMPTS: dict[str, int] = {}


# The default single replica keeps the per-token attempt counter local: the
# redelivered queue entry returns to the same long-lived function worker.
@chalkcompute.function(
    name=os.environ.get("CHALK_E2E_FUNCTION_NAME", "ex-retry"),
    retries=1,
)
def retry_once(token: str) -> str:
    """Fail the first attempt for each token and succeed on its retry."""
    attempt = _ATTEMPTS.get(token, 0) + 1
    _ATTEMPTS[token] = attempt

    if attempt == 1:
        raise RuntimeError(f"intentional first failure for {token}")
    return f"{token}:attempt-{attempt}"
