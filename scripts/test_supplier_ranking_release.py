#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Run the supplier example with a published SDK in an isolated uv environment.

Requires uv, Chalk credentials, and CHALK_ROUTER_API_KEY in the target
environment. Creates a new dataset, suite, evaluation, and two functions, makes
three model calls, and retains resources for inspection (including billable
function capacity). Does not change existing resources or gateway settings.
"""

import argparse
import os
from pathlib import Path
import re
import subprocess


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sdk-version", required=True, help="Exact published version, e.g. 1.2.3"
    )
    parser.add_argument("--environment", required=True, help="Chalk environment ID")
    parser.add_argument(
        "--api-server", required=True, help="Chalk API URL for the target environment"
    )
    parser.add_argument("--model", default="openai/gpt-4.1-mini")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9][0-9A-Za-z.!+_-]*", args.sdk_version):
        parser.error(
            "--sdk-version must be an exact version, not a URL or version range"
        )

    env = os.environ.copy()
    for key in (
        "CHALK_FUNCTION_LOCAL_SDK_COPY",
        "CHALK_FUNCTION_LOCAL_SDK_WHEEL",
        "CHALK_FUNCTION_REMOTE_CALL_WHEEL",
        "CHALK_FUNCTION_STRIP_SOURCE",
        "SUPPLIER_EVAL_NAME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
    ):
        env.pop(key, None)
    env.update(
        CHALK_ENVIRONMENT=args.environment,
        CHALK_ENVIRONMENT_ID=args.environment,
        CHALK_ACTIVE_ENVIRONMENT=args.environment,
        CHALK_API_SERVER=args.api_server,
        SUPPLIER_MODEL=args.model,
    )
    example = (
        Path(__file__).resolve().parents[1]
        / "examples/supplier-ranking/supplier_ranking.py"
    )
    subprocess.run(
        [
            "uv",
            "run",
            "--isolated",
            "--no-project",
            "--with",
            f"chalkcompute=={args.sdk_version}",
            "--with",
            "pyarrow>=14",
            "python",
            "-I",
            str(example),
        ],
        env=env,
        check=True,
    )


if __name__ == "__main__":
    main()
