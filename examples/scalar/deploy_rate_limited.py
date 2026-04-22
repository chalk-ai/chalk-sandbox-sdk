"""Deploy a rate-limited scalar function for testing.

Rate limit: 5 requests per second. The catalog consumer will enforce this,
so calls beyond 5/s will be queued and throttled.

    CHALK_FUNCTION_STRIP_SOURCE=1 uv run python examples/scalar/deploy_rate_limited.py

    Optional: pass --function-name to customize the deployed function name.
"""

from __future__ import annotations

import argparse
import logging

import chalkcompute
from chalkcompute._rate_limit import RateLimitPolicy

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy rate-limited scalar function")
    parser.add_argument(
        "--function-name",
        default="add-rate-limited",
        help="Name for the deployed function",
    )
    args = parser.parse_args()

    @chalkcompute.function(
        name=args.function_name,
        rate_limit=RateLimitPolicy(rate=5, per="second"),
    )
    def add_rate_limited(a: int, b: int) -> int:
        """Simple addition with a 5 req/s rate limit."""
        return a + b


if __name__ == "__main__":
    main()
