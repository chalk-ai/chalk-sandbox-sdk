#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["chalkcompute", "harbor", "dockerfile-parse"]
# ///
"""Stage Enterprise-Bench/l1-l2-bench for Chalk sandboxes and print the harbor command.

    ./prepare.py <downloaded l1-l2-bench dir> [--volume NAME]

The dataset's own setup assumes one Docker host: a locally built base image, and MCP tool
servers on the host reached through host.docker.internal. A Chalk sandbox has neither, so:

* the base image is built by the Chalk image service from the dataset's Dockerfile, with
  the three MCP servers baked in alongside the submission API;
* the ~33 MB of benchmark data goes into a Chalk volume mounted at /data, instead of being
  copied into every image;
* a start script (the sandbox entrypoint) launches the submission API and the MCP servers
  inside each task sandbox, so the agent reaches them on localhost.

The MCP servers only need their MCP processes: each mcp_server.py imports its server
module's data in-process, so the REST ports (9001-9003) are not started.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import tempfile
import zipfile
from pathlib import Path

import chalkcompute

from chalk_harbor.environment import _image_from_dockerfile

BASE_IMAGE_REF = "enterprise-bench/conversational-base:latest"
MCP_SERVERS = {"pm": 8011, "crm": 8012, "file-server": 8013}
BUNDLE = "/opt/enterprise-bench"

START_SH = (
    """#!/bin/sh
# Sandbox entrypoint: the submission API the agent posts its answer to, plus the MCP tool
# servers, all on localhost. Mirrors the base image's ENTRYPOINT and the dataset's compose file.
# The image service drops build-time writes under /workspace; the adapter stages COPYs
# aimed there under /opt/.harbor-workspace, and the API imports from /workspace at boot.
mkdir -p /workspace && cp -an /opt/.harbor-workspace/. /workspace/ && chmod -R 777 /workspace
cd /workspace
uvicorn app.main:app --host 0.0.0.0 --port 8000 >> /agent-logs/conversational/uvicorn.log 2>&1 &
"""
    + "".join(
        f"(cd {BUNDLE}/mcp/{name} && DATA_DIR=/data MCP_PORT={port} FASTMCP_HTTP_HOST_ORIGIN_PROTECTION=false "
        f"exec {BUNDLE}/venv/bin/python mcp_server.py >> /agent-logs/mcp-{name}.log 2>&1) &\n"
        for name, port in MCP_SERVERS.items()
    )
    + "exec sleep infinity\n"
)


def build_image(dataset_dir: Path) -> str:
    with tempfile.TemporaryDirectory() as scratch:
        context = Path(scratch)
        with zipfile.ZipFile(dataset_dir / "base-image.zip") as archive:
            archive.extractall(context)
        context = context / "base-image"
        for name in MCP_SERVERS:
            target = context / "bundle" / "mcp" / name
            target.mkdir(parents=True)
            with zipfile.ZipFile(dataset_dir / "mcp-servers.zip") as archive:
                for member in ("server.py", "mcp_server.py"):
                    target.joinpath(member).write_bytes(
                        archive.read(f"mcp-servers/{name}/{member}")
                    )
        (context / "bundle" / "start.sh").write_text(START_SH)

        dockerfile = context / "Dockerfile"
        lines = [
            line
            for line in dockerfile.read_text().splitlines()
            # The sandbox entrypoint replaces these; see START_SH.
            if not line.startswith(("ENTRYPOINT", "CMD"))
        ]
        lines += [
            f"COPY bundle/ {BUNDLE}/",
            # A separate venv: fastmcp pulls an mcp release that conflicts with the judge's pin.
            " && ".join(
                [
                    f"RUN python -m venv {BUNDLE}/venv",
                    f"{BUNDLE}/venv/bin/pip install --no-cache-dir 'fastapi>=0.104.0' 'uvicorn[standard]>=0.24.0' 'fastmcp>=2.3.0'",
                    f"chmod +x {BUNDLE}/start.sh",
                ]
            ),
            # Harbor's codex installer apt-installs Debian's nodejs/npm, which conflicts with
            # the NodeSource node this image already has; it skips installing when codex exists.
            "RUN npm install -g @openai/codex && apt-get update && apt-get install -y --no-install-recommends ripgrep && rm -rf /var/lib/apt/lists/*",
        ]
        dockerfile.write_text("\n".join(lines) + "\n")
        image = _image_from_dockerfile(dockerfile, {})
        print(
            "building base image (first build installs node, claude-code and python deps)...",
            flush=True,
        )
        return chalkcompute.build_image(image)


def upload_data(dataset_dir: Path, volume_name: str) -> None:
    client = chalkcompute.VolumeClient.from_env()
    try:
        try:
            volume = client.lookup(volume_name)
            print(f"volume {volume_name} exists; overwriting files", flush=True)
        except chalkcompute.VolumeNotFoundError:
            volume = client.create(volume_name)
        with tempfile.TemporaryDirectory() as scratch:
            with zipfile.ZipFile(dataset_dir / "data.zip") as archive:
                archive.extractall(scratch)
            volume.put_dir(scratch)
        print(f"uploaded benchmark data to volume {volume_name}", flush=True)
    finally:
        client.close()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("dataset_dir", type=Path)
    parser.add_argument("--volume", default="harbor-enterprise-bench-data")
    args = parser.parse_args(argv)
    dataset_dir: Path = args.dataset_dir.resolve()

    upload_data(dataset_dir, args.volume)
    image_uri = build_image(dataset_dir)
    print(f"base image: {image_uri}", flush=True)

    mcp_config = dataset_dir / "mcp.chalk.json"
    servers = json.loads((dataset_dir / "mcp.json").read_text())
    for name, port in zip(servers["mcpServers"], MCP_SERVERS.values(), strict=True):
        # Not "localhost": codex tries ::1 first and the servers listen on IPv4 only.
        servers["mcpServers"][name]["url"] = f"http://127.0.0.1:{port}/mcp"
    mcp_config.write_text(json.dumps(servers, indent=2) + "\n")

    ek = [
        f"image_map={BASE_IMAGE_REF}={image_uri}",
        f"volumes={args.volume}:/data",
        "entrypoint=" + json.dumps(["sh", f"{BUNDLE}/start.sh"]),
        # The servers load ~25 MB of JSON at boot and the entrypoint runs asynchronously;
        # hold the agent until the submission API and all three MCP ports answer.
        "ready_command="
        + " && ".join(
            ["curl -sf -o /dev/null 127.0.0.1:8000/health"]
            + [
                f"curl -s -o /dev/null 127.0.0.1:{port}/mcp"
                for port in MCP_SERVERS.values()
            ]
        ),
    ]
    command = [
        "harbor", "run", "-p", str(dataset_dir), "-i", "eng-l1-a",
        "-a", "codex", "-m", "openai/gpt-5-mini", "--mcp-config", str(mcp_config),
        "-e", "chalk_harbor:ChalkSandboxEnvironment",
        *[part for kv in ek for part in ("--ek", kv)],
        "-n", "1", "--yes",
    ]  # fmt: skip
    print(
        "\nrun it with OPENAI_API_KEY set (codex uses it, and so does the judge, openai/gpt-5):\n"
    )
    print("  " + shlex.join(command))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
