"""A simple async-generator function deployed with @chalkcompute.function.

The function yields integers one at a time so callers can stream results
via chalkdf's ``Remote(...).function(name)`` iterator protocol.
"""

import argparse
import logging
from collections.abc import AsyncIterator

import chalkcompute


logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy async count-up generator")
    parser.add_argument(
        "--function-name",
        default="chalk-sandbox-test-count-up",
        help="Name for the deployed function",
    )
    args = parser.parse_args()

    @chalkcompute.function(
        name=args.function_name,
        image=chalkcompute.Image.debian_slim(),
    )
    async def count_up(n: int) -> AsyncIterator[int]:
        for i in range(n):
            log.info("Yielding %d", i)
            yield i


if __name__ == "__main__":
    main()
