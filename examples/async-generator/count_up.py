"""A simple async-generator function deployed with @chalkcompute.function.

The function yields integers one at a time so callers can stream results
via chalkdf's ``Remote(...).function(name)`` iterator protocol.
"""

import logging
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import chalkcompute

# Add examples dir to path so we can import test_utils
sys.path.insert(0, str(Path(__file__).parent.parent))
from test_utils import make_function_name

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


@chalkcompute.function(
    name=make_function_name("count-up"),
    image=chalkcompute.Image.debian_slim(),
)
async def count_up(n: int) -> AsyncIterator[int]:
    for i in range(n):
        log.info("Yielding %d", i)
        yield i
