"""Deploy an async scalar function for testing.

    CHALK_FUNCTION_STRIP_SOURCE=1 uv run python examples/scalar/deploy_async.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import chalkcompute

# Add examples dir to path so we can import test_utils
sys.path.insert(0, str(Path(__file__).parent.parent))
from test_utils import make_function_name

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


@chalkcompute.function(name=make_function_name("async-add"))
async def async_add(a: int, b: int) -> int:
    """Simple async addition."""
    return a + b
