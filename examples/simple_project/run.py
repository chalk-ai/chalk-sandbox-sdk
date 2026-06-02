"""
Build a container from ~/chalk/simple_project with opencode installed,
then exec into it and stream the output.

Usage:
    # Ensure .env is decrypted first
    just env
    uv run python examples/simple_project/run.py
"""

from __future__ import annotations

import os
import sys

# Load .env for credentials
env_path = os.path.join(os.path.dirname(__file__), "../../.env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

from chalkcompute import Container, Image

PROJECT_DIR = os.path.expanduser("~/chalk/simple_project")

# 1. Build the image: Python slim + local project dir + install opencode
image = (
    Image.debian_slim()
    .add_local_dir(PROJECT_DIR, "/app/simple_project", strategy="copy")
    .run_commands(
        "pip install --quiet opencode-ai",
    )
    .workdir("/app/simple_project")
)

# 2. Construct and run the container
container = Container(image=image, lifetime="600s")
container.run()

# 3. Verify the project files and opencode are installed, then stream output
print("\n=== Project files ===")
result = container.exec("ls", "-la", "/app/simple_project")
print(result.stdout_text)

print("=== Verify opencode is installed ===")
result = container.exec("pip", "show", "opencode-ai")
print(result.stdout_text)
if result.exit_code != 0:
    print(f"opencode install failed: {result.stderr_text}", file=sys.stderr)

print("=== Run the project ===")
result = container.exec("python", "foo.py", timeout_secs=30)
print(result.stdout_text)
if result.stderr_text:
    print(result.stderr_text, file=sys.stderr)

# 4. Fetch logs via chalk CLI
print("\n=== Container logs ===")
import subprocess

chalk_env = {**os.environ}
if "CHALK_ENVIRONMENT_ID" in chalk_env:
    chalk_env["CHALK_ACTIVE_ENVIRONMENT"] = chalk_env["CHALK_ENVIRONMENT_ID"]

if container.info is not None:
    logs_result = subprocess.run(
        [
            "chalk",
            "logs",
            "--query",
            f"pod_name:{container.info.pod_name}",
            "--start-time",
            "10m ago",
        ],
        check=False,
        text=True,
        capture_output=True,
        env=chalk_env,
    )
    print(logs_result.stdout)
    if logs_result.stderr:
        print(logs_result.stderr, file=sys.stderr)

# Clean up
container.stop()
print("Done.")
