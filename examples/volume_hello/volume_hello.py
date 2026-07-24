#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["chalkcompute", "httpx", "python-dotenv"]
# ///

# # Volume Hello World
#
# This example shows the core Chalk volume workflow: upload a file to a named
# persistent volume, mount that volume in a container, and read the file from
# inside. Volumes outlive containers — data written in one run is available to
# every subsequent run, making them ideal for model weights, datasets, or any
# artifact that's expensive to regenerate.
#
# **Steps:**
# 1. Create a named volume and upload `hello.txt` into it.
# 2. Start a container with the volume mounted at `/mnt/data`.
# 3. Read `/mnt/data/hello.txt` from inside the container via `exec`.
# 4. Stop the container (which also deletes the temporary volume).

import base64
import logging
import os
import uuid

import httpx
from dotenv import load_dotenv

from chalkcompute import Container

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

MOUNT_PATH = "/mnt/data"
FILE_NAME = "hello.txt"
FILE_CONTENT = "Hello from a Chalk volume!\n"


# ## Authentication
#
# Volume management and container operations both authenticate with short-lived
# tokens from Chalk's OAuth endpoint. We fetch one token and share it across
# all direct API calls; the Container client fetches its own token internally.


def _get_token() -> str:
    resp = httpx.post(
        f"{os.environ['CHALK_API_SERVER']}/v1/oauth/token",
        json={
            "client_id": os.environ["CHALK_CLIENT_ID"],
            "client_secret": os.environ["CHALK_CLIENT_SECRET"],
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _volume_rpc(method: str, body: dict, *, token: str) -> httpx.Response:
    return httpx.post(
        f"{os.environ['CHALK_API_SERVER']}/chalk.volume.v1.VolumeService/{method}",
        json=body,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Chalk-Env-Id": os.environ["CHALK_ENVIRONMENT_ID"],
            "Content-Type": "application/json",
        },
        timeout=30,
    )


# ## Step 1 — Create a volume and upload a file
#
# `CreateVolume` is idempotent — a 409 means the volume already exists, which
# is fine. File data is transmitted as base64 in the JSON body.


def create_volume(name: str, token: str) -> None:
    resp = _volume_rpc("CreateVolume", {"name": name}, token=token)
    if resp.status_code != 409:
        resp.raise_for_status()
    log.info("Volume '%s' ready.", name)

    resp = _volume_rpc(
        "PutFile",
        {
            "volumeName": name,
            "path": FILE_NAME,
            "data": base64.b64encode(FILE_CONTENT.encode()).decode(),
        },
        token=token,
    )
    resp.raise_for_status()
    log.info("Uploaded '%s' (%d bytes).", FILE_NAME, len(FILE_CONTENT))


# ## Step 2 — Run a container with the volume mounted
#
# The `volumes` parameter accepts `(volume_name, mount_path)` pairs. Chalk
# mounts each volume via a ChalkFS sidecar — a FUSE process that syncs files
# from object storage into the container's filesystem.
#
# `Container.stop()` also deletes volumes listed in `volumes`, so no separate
# cleanup call is needed.


def main() -> None:
    run_id = uuid.uuid4().hex[:8]
<<<<<<< Updated upstream
    vol_name = f"hello-vol-{run_id}"
=======
    vol_name = "ariscoolvolume"
    container_name = f"hello-container-{run_id}"
>>>>>>> Stashed changes

    # token = _get_token()
    # create_volume(vol_name, token)

<<<<<<< Updated upstream
=======
    env: dict[str, str] = {}
    # if client_id := os.environ.get("CHALK_CLIENT_ID"):
    #     env["CHALK_CLIENT_ID"] = client_id
    # if client_secret := os.environ.get("CHALK_CLIENT_SECRET"):
    #     env["CHALK_CLIENT_SECRET"] = client_secret

>>>>>>> Stashed changes
    container = Container(
        image="python:3.14-slim-trixie",
        volumes=[(vol_name, MOUNT_PATH)],
    )
    # try:
    container.run()

        # ## Step 3 — Read the file from inside the container
        #
        # `exec` runs a command synchronously. The volume is served by ChalkFS,
        # so `/mnt/data/hello.txt` appears as an ordinary filesystem path — no
        # special client or protocol needed inside the container.

    #     result = container.exec("cat", f"{MOUNT_PATH}/{FILE_NAME}")
    #     if result.exit_code != 0:
    #         raise RuntimeError(
    #             f"cat failed (exit {result.exit_code}): {result.stderr_text}"
    #         )
    #     print(f"\nRead from container: {result.stdout_text.strip()!r}")

    # finally:
    #     log.info("Stopping container and cleaning up volume...")
    #     container.stop()


if __name__ == "__main__":
    main()
