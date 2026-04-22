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
import sys
from pathlib import Path

import pyarrow as pa
from chalkdf import Remote

# Add examples dir to path so we can import test_utils
sys.path.insert(0, str(Path(__file__).parent.parent))
from test_utils import make_function_name

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

async def main() -> None:
    remote = Remote()
    fn = remote.function(make_function_name("df-groupby-sum"), output_type=pa.int64())
    log.info("Calling remote function %s...", make_function_name("df-groupby-sum"))
    result = await fn(1)
    log.info("Result: %s", result)
    print(f"Remote computed grand total: {result}")


if __name__ == "__main__":
    asyncio.run(main())
