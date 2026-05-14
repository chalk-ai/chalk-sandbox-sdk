"""A simple async-generator function deployed with @chalkcompute.function.

The function yields integers one at a time so callers can stream results
"""

import logging
from collections.abc import AsyncIterator

import chalkcompute


logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


@chalkcompute.function(
    image=chalkcompute.Image.debian_slim(),
)
async def count_up(n: int) -> AsyncIterator[int]:
    for i in range(n):
        log.info("Yielding %d", i)
        yield i
