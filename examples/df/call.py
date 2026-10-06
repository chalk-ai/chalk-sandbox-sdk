#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute>=2.11.1"]
# ///

"""Call the deployed df-groupby-sum function with the public Chalk Compute API.

Prerequisites:
  1. Deploy the function first.
  2. Set the standard Chalk credentials.
"""

import logging

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


def main() -> None:
    fn = chalkcompute.RemoteFunction.from_name("df-groupby-sum")
    fn.wait_ready()
    log.info("Calling remote function df-groupby-sum...")
    result = fn.remote(1)
    log.info("Result: %s", result)
    print(f"Remote computed grand total: {result}")


if __name__ == "__main__":
    main()
