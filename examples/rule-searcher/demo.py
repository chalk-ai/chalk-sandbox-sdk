#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute", "python-dotenv"]
# ///

import logging
import os
import uuid
from pathlib import Path

from chalkcompute._connect import ConnectClient
from dotenv import load_dotenv

from chalkcompute import Container, ContainerError, Image, VolumeClient

for noisy in ("chalkcompute", "httpx", "chalkcompute._volume", "chalkcompute._container"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

load_dotenv()

NAME = f"rule-searcher-{uuid.uuid4().hex[:6]}"
AGENT_CODE = Path("examples/rule-searcher/worker.py").read_text()


# ─── 1. The Image ──────────────────────────────────────────────────────────────
# Slim Python + git + the chalk CLI + the Anthropic SDK. Built once, cached.

image = (
    Image.base("python:3.12-slim")
    .run_commands(
        "apt-get update -qq && apt-get install -y -qq git curl",
        "curl -fsSL https://api.chalk.ai/install.sh | sh",
    )
    .pip_install(["anthropic>=0.40.0", "chalkpy>=2.0.0"])
    .env({"PATH": "/root/.chalk/bin:/usr/local/bin:/usr/bin:/bin"})
)


# ─── 2. The Volume ─────────────────────────────────────────────────────────────
# Shared persistent storage. The agent code lives here, and the agent writes
# its discoveries here.

print(f"\n  Creating volume   {NAME}")
volume = VolumeClient.from_connect(ConnectClient()).create(NAME)
volume.put_file("agent.py", AGENT_CODE)


# ─── 3. The Container ──────────────────────────────────────────────────────────
# Boot the image, mount the volume, run the agent.

print(f"  Logs              chalk logs --query 'pod_name:chalk-container-{NAME}' --start-time '30m ago' --follow")
print(f"  Starting agent    (~2 min first run, cached after)")
container = Container(
    image=image,
    name=NAME,
    volumes=[(NAME, "/data")],
    entrypoint=["python", "/data/agent.py"],
    lifetime="1800s",
    env={
        "WORKER_ID": "0",
        "PROJECT_REPO": "chalk-ai/fraud-template",
        "PRIMARY_KEY_FEATURE": "user.id",
        "SAMPLE_INPUTS": ",".join(str(i) for i in range(1, 31)),
        "PROMOTIONS_DIR": "/data/promotions",
        **{k: os.environ[k] for k in [
            "ANTHROPIC_API_KEY", "GITHUB_TOKEN",
            "CHALK_CLIENT_ID", "CHALK_CLIENT_SECRET", "CHALK_API_SERVER",
            "CHALK_ACTIVE_ENVIRONMENT", "CHALK_ENVIRONMENT_ID",
        ] if k in os.environ},
    },
)
try:
    container.run()
except ContainerError as e:
    # Container can finish (Succeeded) before .run() polls and sees Running —
    # treat that as success and re-attach by name.
    if "Succeeded" not in str(e):
        raise
    container = Container.from_name(NAME)
container._volumes = []  # keep the volume after stop()


# ─── Done. ─────────────────────────────────────────────────────────────────────

print(f"\n  Done.")
print(f"  Container   {container.id}")
print(f"  Volume      {NAME}")
print(f"  Trace       chalk volume get-file --name {NAME} --path traces/worker-0.jsonl")
print(f"  Results     chalk volume get-file --name {NAME} --path promotions/worker-0.jsonl\n")
