"""Deploy the sync generator function.

uv run python examples/generator/deploy_sync.py
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import chalkcompute

# Add examples dir to path so we can import test_utils
sys.path.insert(0, str(Path(__file__).parent.parent))
from test_utils import make_function_name

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


@chalkcompute.function(name=make_function_name("count-up"))
def count_up(n: int) -> Iterator[int]:
    """Sync generator: yields integers 0..n-1."""
    for i in range(n):
        yield i
