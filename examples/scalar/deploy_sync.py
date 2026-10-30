#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute>=2.11.1"]
# ///

"""Deploy a sync scalar function for testing.

The deployment intentionally persists for subsequent remote calls and for the
nested-gather example that invokes it by name.
"""

from __future__ import annotations

import logging
import os

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


@chalkcompute.function(name=os.environ.get("CHALK_E2E_FUNCTION_NAME", "ex-add"))
def add(a: int, b: int) -> int:
    """Simple addition."""
    return a + b
