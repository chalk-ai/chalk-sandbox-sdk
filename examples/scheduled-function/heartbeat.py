"""Deploy a scheduled remote function for testing.

A scheduled function takes no inputs and is invoked automatically by the
platform on the given schedule (cron expression or shorthand like ``"10m"``).
This example registers a heartbeat that fires every ten minutes — the body
just logs a timestamp so the schedule's effect is observable in the pod logs.

    uv run python examples/scheduled-function/heartbeat.py
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

FUNCTION_NAME = os.getenv("CHALK_HEARTBEAT_FUNCTION_NAME", "heartbeat")


def _with_function_name(func):
    func.__name__ = FUNCTION_NAME
    globals()[FUNCTION_NAME] = func
    return func


@chalkcompute.function(
    schedule="10m",
    env={"CHALK_HEARTBEAT_FUNCTION_NAME": FUNCTION_NAME},
)
@_with_function_name
def heartbeat() -> None:
    """Log a timestamp each time the schedule fires."""
    log.info("heartbeat at %s", datetime.now(timezone.utc).isoformat())
