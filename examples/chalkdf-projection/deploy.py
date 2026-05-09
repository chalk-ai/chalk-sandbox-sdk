#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx"]
# ///

"""Deploy the ChalkDF projection example container via `chalk container run`, then tail logs."""

import base64
import os
import subprocess
import sys
import time

CONTAINER_NAME = "chalkdf-projection-example"

SCRIPT = """\
import pyarrow as pa
from chalkdf import DataFrame

table = pa.table({
    "user_id": [1, 2, 3],
    "distance_km": [0.5, 1.2, 3.7],
    "active": [True, False, True],
})

df = DataFrame(table)
print("Input DataFrame:")
print(df)

projected = df.select("user_id", "distance_km").run()
print()
print("Projected DataFrame (user_id, distance_km):")
print(projected)
"""

# Path to .env with Chalk credentials (relative to repo root)
ENV_FILE = os.path.join(os.path.dirname(__file__), "../../.env")


def run(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    print(f"+ {' '.join(args)}")
    return subprocess.run(args, check=check, text=True, capture_output=True)


def load_env(path: str) -> dict[str, str]:
    """Load .env file and map CHALK_ENVIRONMENT_ID to CHALK_ACTIVE_ENVIRONMENT."""
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    # The chalk CLI uses CHALK_ACTIVE_ENVIRONMENT, not CHALK_ENVIRONMENT_ID
    if "CHALK_ENVIRONMENT_ID" in env:
        env["CHALK_ACTIVE_ENVIRONMENT"] = env.pop("CHALK_ENVIRONMENT_ID")
    return env


def main() -> None:
    # Load credentials into environment for the chalk CLI
    creds = load_env(ENV_FILE)
    env = {**os.environ, **creds}

    # Stop any previous run
    print(f"+ chalk container stop --name {CONTAINER_NAME}")
    subprocess.run(
        ["chalk", "container", "stop", "--name", CONTAINER_NAME],
        check=False, text=True, capture_output=True, env=env,
    )

    # Encode the script as base64 to avoid shell escaping issues
    b64 = base64.b64encode(SCRIPT.encode()).decode()
    shell_cmd = (
        "pip install --quiet chalkdf pyarrow chalkpy"
        " && python3 -c \"import base64 as b; exec(b.b64decode('"
        + b64
        + "').decode())\""
    )

    print(f"+ chalk container run --image python:3.12-slim-bookworm --name {CONTAINER_NAME} ...")
    result = subprocess.run(
        [
            "chalk", "container", "run",
            "--image", "python:3.12-slim-bookworm",
            "--name", CONTAINER_NAME,
            "--entrypoint", f"bash,-c,{shell_cmd}",
            "--lifetime", "5m",
        ],
        check=False, text=True, capture_output=True, env=env,
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    if result.returncode != 0:
        print(f"chalk container run failed with exit code {result.returncode}", file=sys.stderr)
        sys.exit(1)

    print(f"\nContainer '{CONTAINER_NAME}' started. Waiting 60s for pip install + run...\n")
    time.sleep(60)

    # Fetch logs
    print(f"+ chalk logs --query pod_name:chalk-container-{CONTAINER_NAME} --start-time 5m ago")
    logs_result = subprocess.run(
        ["chalk", "logs", "--query", f"pod_name:chalk-container-{CONTAINER_NAME}", "--start-time", "5m ago"],
        check=False, text=True, capture_output=True, env=env,
    )
    print(logs_result.stdout)
    if logs_result.stderr:
        print(logs_result.stderr, file=sys.stderr)


if __name__ == "__main__":
    main()
