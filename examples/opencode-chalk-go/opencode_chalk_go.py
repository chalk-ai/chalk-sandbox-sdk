#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute", "python-dotenv"]
# ///

# # OpenCode on chalk-go
#
# Launch an [OpenCode](https://opencode.ai) server in a Chalk container with
# the `chalk-go` repo pre-loaded from a persistent volume. The volume
# (`chalk-go-gh`) must already exist — populate it first with:
#
#     python scripts/populate_chalk_go_volume.py
#
# Then run this example:
#
#     python examples/opencode-chalk-go/opencode_chalk_go.py
#
# The container mounts the volume at `/root/code` so OpenCode opens directly
# into the chalk-go source tree. No git clone at startup — the files are
# already there via ChalkFS.
#
# ## Environment variables
#
#     OPENCODE_SERVER_PASSWORD  Optional password for the server.
#     ANTHROPIC_API_KEY         Forwarded into the container.
#     OPENAI_API_KEY            Forwarded into the container.

import argparse
import logging
import os
import time
import uuid

from chalkcompute import Container, Image
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()
# Pick up ANTHROPIC_API_KEY from chalk-router/.env (won't override existing vars)
_router_env = Path.home() / "chalk" / "chalk-router" / ".env"
if _router_env.exists():
    load_dotenv(_router_env)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

OPENCODE_PORT = 4096
OPENCODE_BIN = "/root/.opencode/bin/opencode"
VOLUME_NAME = "chalk-go-gh"
MOUNT_PATH = "/root/code"

# ## Image — Go toolchain + opencode
#
# We start from a Go image so the LSP and build tools work out of the box,
# then layer on curl + opencode.

OPENCODE_GO_IMAGE = (
    Image.base("golang:1.26-trixie")
    .run_commands(
        "apt-get update -qq && apt-get install -y -qq curl git 2>&1",
        "curl -fsSL https://opencode.ai/install | bash 2>&1",
    )
)


def start_server(container: Container) -> None:
    """Kill the placeholder HTTP server and start opencode serve."""
    log.info("Stopping placeholder HTTP server on port %d...", OPENCODE_PORT)
    container.exec(
        "bash", "-c",
        "kill $(cat /tmp/http_server.pid 2>/dev/null) 2>/dev/null; "
        "rm -f /tmp/http_server.pid; true",
    )

    log.info("Starting opencode server on port %d...", OPENCODE_PORT)
    # Write a launcher script so the exec returns immediately
    container.exec(
        "bash", "-c",
        "cat > /tmp/start_opencode.sh << 'SCRIPT'\n"
        "#!/bin/bash\n"
        f"cd {MOUNT_PATH}\n"
        f"exec {OPENCODE_BIN} serve --hostname=0.0.0.0 --port={OPENCODE_PORT} --log-level=DEBUG\n"
        "SCRIPT\n"
        "chmod +x /tmp/start_opencode.sh",
        timeout_secs=10,
    )
    container.exec(
        "bash", "-c",
        "nohup /tmp/start_opencode.sh > /tmp/opencode.log 2>&1 &",
        timeout_secs=10,
    )

    log.info("Waiting for opencode to be ready...")
    for attempt in range(60):
        result = container.exec(
            "bash", "-c",
            f"curl -s http://localhost:{OPENCODE_PORT} > /dev/null 2>&1; echo $?",
            timeout_secs=10,
        )
        if result.stdout_text.strip() == "0":
            return
        if attempt < 59:
            time.sleep(2)
    raise RuntimeError(
        "opencode server did not become ready after 2 minutes.\n"
        "Check /tmp/opencode.log inside the container for details."
    )


def main(
    container_name: str,
    password: str | None,
    timeout_secs: int,
) -> None:
    env: dict[str, str] = {}
    if password:
        env["OPENCODE_SERVER_PASSWORD"] = password
    if anthropic_key := os.environ.get("ANTHROPIC_API_KEY"):
        env["ANTHROPIC_API_KEY"] = anthropic_key
    if openai_key := os.environ.get("OPENAI_API_KEY"):
        env["OPENAI_API_KEY"] = openai_key

    container = Container(
        image=OPENCODE_GO_IMAGE,
        name=container_name,
        env=env,
        port=OPENCODE_PORT,
        volumes=[(VOLUME_NAME, MOUNT_PATH)],
        entrypoint=[
            "bash", "-c",
            f"python3 -m http.server {OPENCODE_PORT} --bind 0.0.0.0 "
            f"&>/dev/null & echo $! > /tmp/http_server.pid; tail -f /dev/null",
        ],
    ).run()

    # Don't let stop() delete the persistent shared volume
    container._volumes = []

    try:
        # Verify volume is mounted
        result = container.exec("ls", MOUNT_PATH)
        log.info("Volume contents: %s", result.stdout_text.strip()[:200])

        start_server(container)

        container.refresh()
        web_url = container.info.web_url if container.info else None
        log.info("--- OpenCode server ready (chalk-go) ---")
        if web_url:
            log.info("  Web UI:  %s", web_url)
            pw_prefix = f"OPENCODE_SERVER_PASSWORD={password} " if password else ""
            log.info("  TUI:     %sopencode attach %s", pw_prefix, web_url)
        else:
            log.info("  Container ID: %s", container.id)
        log.info("----------------------------------------")

        mins = timeout_secs // 60
        log.info("Server running. Press Ctrl-C to stop (timeout: %dm).", mins)
        time.sleep(timeout_secs)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        container.stop()
        log.info("Container stopped.")


def _parse_timeout(value: str) -> int:
    if value.endswith("h"):
        return int(value[:-1]) * 3600
    if value.endswith("m"):
        return int(value[:-1]) * 60
    return int(value) * 3600


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Launch OpenCode on chalk-go in a Chalk container",
    )
    parser.add_argument(
        "--timeout", type=str, default="12",
        help="Server lifetime (e.g. 2h, 90m). Bare number = hours. Default: 12",
    )
    parser.add_argument(
        "--name", type=str,
        default=f"opencode-chalk-go-{uuid.uuid4().hex[:8]}",
        help="Container name",
    )
    parser.add_argument(
        "--password", type=str,
        default=os.environ.get("OPENCODE_SERVER_PASSWORD"),
        help="OpenCode server password",
    )
    args = parser.parse_args()
    main(
        container_name=args.name,
        password=args.password,
        timeout_secs=_parse_timeout(args.timeout),
    )
