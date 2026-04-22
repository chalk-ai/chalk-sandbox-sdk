"""Deploy a concurrency-limited scalar function for testing.

Concurrency limit: 2 concurrent calls. The catalog consumer will enforce
this via a semaphore, so at most 2 calls execute simultaneously.

    CHALK_FUNCTION_STRIP_SOURCE=1 uv run python examples/scalar/deploy_concurrency_limited.py
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import chalkcompute
from chalkcompute._concurrency import ConcurrencyPolicy

# Add examples dir to path so we can import test_utils
sys.path.insert(0, str(Path(__file__).parent.parent))
from test_utils import make_function_name

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


@chalkcompute.function(
    name=make_function_name("slow-add"),
    concurrency=ConcurrencyPolicy(max_concurrent=2),
)
def slow_add(a: int, b: int) -> int:
    """Addition with a 1-second sleep to make concurrency limiting observable."""
    time.sleep(1)
    return a + b
