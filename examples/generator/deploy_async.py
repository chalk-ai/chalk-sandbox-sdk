"""Deploy the async generator function.

    uv run python examples/generator/deploy_async.py

    Optional: pass --function-name to customize the deployed function name.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import AsyncIterator

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy async generator function")
    parser.add_argument(
        "--function-name",
        default="async-count-up",
        help="Name for the deployed function",
    )
    args = parser.parse_args()

    @chalkcompute.function(name=args.function_name)
    async def async_count_up(n: int) -> AsyncIterator[int]:
        """Async generator: yields integers 0..n-1 with simulated async work."""
        import asyncio

        for i in range(n):
            await asyncio.sleep(0.01)
            yield i


if __name__ == "__main__":
    main()
