"""Deploy the sync generator function.

uv run python examples/generator/deploy_sync.py

Optional: pass --function-name to customize the deployed function name.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Iterator

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy sync generator function")
    parser.add_argument(
        "--function-name",
        default="count-up",
        help="Name for the deployed function",
    )
    args = parser.parse_args()

    @chalkcompute.function(name=args.function_name)
    def count_up(n: int) -> Iterator[int]:
        """Sync generator: yields integers 0..n-1."""
        for i in range(n):
            yield i


if __name__ == "__main__":
    main()
