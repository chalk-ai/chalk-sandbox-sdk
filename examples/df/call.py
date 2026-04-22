"""Call the deployed df-groupby-sum function from a local client using chalkdf.Remote.

Prerequisites:
  1. Deploy the function first:  uv run python examples/df/deploy.py
  2. Ensure CHALK_CLIENT_ID / CHALK_CLIENT_SECRET / CHALK_API_SERVER / CHALK_ENVIRONMENT_ID are set.

Usage:
    source ~/chalk/chalk-sandbox-sdk/.env
    cd ~/chalk/chalk-private/dataframe && uv run python ~/chalk/chalk-sandbox-sdk/examples/df/call.py
"""

import asyncio
import logging
import os
import subprocess

import pyarrow as pa
from chalkdf import Remote

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


def _get_test_run_hash() -> str:
    """Get a short hash for this test run, only in CI."""
    if not os.environ.get("CI") and not os.environ.get("BUILDKITE"):
        return ""
    if hash_env := os.environ.get("CHALK_TEST_RUN_HASH"):
        return hash_env[:8]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()[:8]
    except Exception:
        pass
    return ""


_HASH = _get_test_run_hash()
_FUNCTION_NAME = f"chalk-sandbox-test-{_HASH}-df-groupby-sum" if _HASH else "chalk-sandbox-test-df-groupby-sum"


async def main() -> None:
    remote = Remote()
    fn = remote.function(_FUNCTION_NAME, output_type=pa.int64())
    log.info("Calling remote function %s...", _FUNCTION_NAME)
    result = await fn(1)
    log.info("Result: %s", result)
    print(f"Remote computed grand total: {result}")


if __name__ == "__main__":
    asyncio.run(main())
