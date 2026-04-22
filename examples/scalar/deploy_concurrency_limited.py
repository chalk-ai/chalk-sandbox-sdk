"""Deploy a concurrency-limited scalar function for testing.

Concurrency limit: 2 concurrent calls. The catalog consumer will enforce
this via a semaphore, so at most 2 calls execute simultaneously.

    CHALK_FUNCTION_STRIP_SOURCE=1 uv run python examples/scalar/deploy_concurrency_limited.py

    Optional: pass --function-name to customize the deployed function name.
"""

from __future__ import annotations

import argparse
import logging
import time

import chalkcompute
from chalkcompute._concurrency import ConcurrencyPolicy

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy concurrency-limited scalar function")
    parser.add_argument(
        "--function-name",
        default="slow-add",
        help="Name for the deployed function",
    )
    args = parser.parse_args()

    @chalkcompute.function(
        name=args.function_name,
        concurrency=ConcurrencyPolicy(max_concurrent=2),
    )
    def slow_add(a: int, b: int) -> int:
        """Addition with a 1-second sleep to make concurrency limiting observable."""
        time.sleep(1)
        return a + b


if __name__ == "__main__":
    main()
