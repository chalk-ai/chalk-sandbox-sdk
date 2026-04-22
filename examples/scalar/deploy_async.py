"""Deploy an async scalar function for testing.

    CHALK_FUNCTION_STRIP_SOURCE=1 uv run python examples/scalar/deploy_async.py

    Optional: pass --function-name to customize the deployed function name.
"""

from __future__ import annotations

import argparse
import logging

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy async scalar function")
    parser.add_argument(
        "--function-name",
        default="async-add",
        help="Name for the deployed function",
    )
    args = parser.parse_args()

    @chalkcompute.function(name=args.function_name)
    async def async_add(a: int, b: int) -> int:
        """Simple async addition."""
        return a + b


if __name__ == "__main__":
    main()
