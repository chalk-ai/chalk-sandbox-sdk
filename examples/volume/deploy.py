#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx"]
# ///

"""
Example: mount a Chalk volume in a container and read a file from it.

Steps:
  1. Create a volume named 'example-vol'
  2. Upload a text file into it
  3. Run a container with --volume=example-vol:/mnt/data
  4. The container cats /mnt/data/hello.txt (the uploaded file)
  5. Clean up: stop the container and delete the volume
"""

import os
import subprocess
import sys
import time

VOLUME_NAME = "example-vol"
CONTAINER_NAME = "volume-mount-example"
MOUNT_PATH = "/mnt/data"
FILE_PATH = "hello.txt"
FILE_CONTENT = "Hello from a Chalk volume!"

ENV_FILE = os.path.join(os.path.dirname(__file__), "../../.env")


def load_env(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    if "CHALK_ENVIRONMENT_ID" in env:
        env["CHALK_ACTIVE_ENVIRONMENT"] = env.pop("CHALK_ENVIRONMENT_ID")
    return env


def chalk(
    args: list[str], env: dict[str, str], check: bool = True
) -> subprocess.CompletedProcess[str]:
    cmd = ["chalk"] + args
    print(f"+ {' '.join(cmd)}")
    result = subprocess.run(cmd, check=check, text=True, capture_output=True, env=env)
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)
    return result


def main() -> None:
    creds = load_env(ENV_FILE)
    env = {**os.environ, **creds}

    # 1. Create the volume (idempotent — ignores "already exists" errors)
    print("=== Creating volume ===")
    chalk(["volume", "create", "--name", VOLUME_NAME], env, check=False)

    # 2. Upload a file into the volume via stdin
    print("\n=== Uploading file to volume ===")
    subprocess.run(
        [
            "chalk", "volume", "put-file",
            "--name", VOLUME_NAME,
            "--path", FILE_PATH,
            "--file", "-",
        ],
        input=FILE_CONTENT,
        check=True, text=True, capture_output=True, env=env,
    )
    print(f"Uploaded '{FILE_PATH}' ({len(FILE_CONTENT)}B) to volume '{VOLUME_NAME}'")

    # 3. Verify the file is in the volume
    print("\n=== Listing volume files ===")
    chalk(["volume", "list-files", "--name", VOLUME_NAME], env)

    # 4. Stop any previous container with the same name
    chalk(["container", "stop", "--name", CONTAINER_NAME], env, check=False)

    # 5. Run a container that mounts the volume and cats the file
    #    The volume is backed by ChalkFS (FUSE) and auto-syncs files from
    #    the volume service.
    print("\n=== Running container with mounted volume ===")
    result = chalk([
        "container", "run",
        "--image", "python:3.12-slim-bookworm",
        "--name", CONTAINER_NAME,
        f"--volume={VOLUME_NAME}:{MOUNT_PATH}",
        "--entrypoint", f"bash,-c,sleep 10 && echo 'Files in {MOUNT_PATH}:' && ls -la {MOUNT_PATH}/ && echo '---' && cat {MOUNT_PATH}/{FILE_PATH}",
        "--lifetime", "5m",
    ], env, check=False)
    if result.returncode != 0:
        print(f"container run exited with code {result.returncode}", file=sys.stderr)
        sys.exit(1)

    # 6. Wait for the container to start and the script to finish
    print("\n=== Waiting 30s for container to run ===")
    time.sleep(30)

    print("\n=== Fetching container logs ===")
    chalk(
        ["logs", "--query", f"pod_name:chalk-container-{CONTAINER_NAME}", "--start-time", "5m ago"],
        env,
        check=False,
    )

    # 7. Clean up
    print("\n=== Cleaning up ===")
    chalk(["container", "stop", "--name", CONTAINER_NAME], env, check=False)
    chalk(["volume", "delete", "--name", VOLUME_NAME], env, check=False)

    print("\nDone!")


if __name__ == "__main__":
    main()
