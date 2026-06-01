#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute"]
# ///
"""Deploy the HITL Slack-approval service as a Chalk ``Container`` workload.

What this does
--------------
1. Reads ``app.py`` (a stdlib-only HTTP service) from this directory.
2. Builds a tiny ``debian_slim`` image (no extra pip deps — the app is stdlib
   only) and starts a ``Container`` that publishes a port. Publishing a port is
   what makes Chalk allocate a public URL and inject it as
   ``CHALK_CONTAINER_WEB_URL`` inside the pod.
3. Passes the Slack bot token in as a Chalk secret (read from the local
   ``SLACK_BOT_TOKEN`` env var — i.e. the ~/chalk/github-pinger bot creds) and
   the channel as a plain env var.
4. The app source is injected via the ``APP_SRC`` env var and ``exec``'d by the
   entrypoint, so there is no local-file/volume sync to wait on.

On boot the container posts an approval request to ``#chalkbot-dev`` with
Approve / Reject buttons that link back to its own public URL.

Run it
------
    set -a && source ~/chalk/github-pinger/.env && set +a   # SLACK_BOT_TOKEN
    set -a && source .env && set +a                          # CHALK_* creds
    uv run python examples/hitl_slack_approval/deploy.py

Pass ``--stop NAME`` to tear a running instance down.
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys

from chalkcompute import Container, Image, Secret

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
APP_PATH = os.path.join(HERE, "app.py")
PORT = 8080
SLACK_CHANNEL = "#chalkbot-dev"


def deploy(name: str) -> None:
    # Normalize the token: some .env files store it quoted
    # (SLACK_BOT_TOKEN="xoxb-..."). If the caller exported it without letting
    # the shell strip those quotes, the literal quotes travel into the pod and
    # Slack rejects the token as invalid_auth. Strip surrounding quotes/space so
    # the deploy is robust regardless of how the var was set. The Chalk Secret
    # is read from os.environ at Container.run() time, so fix it here.
    raw_token = os.environ.get("SLACK_BOT_TOKEN", "").strip().strip('"').strip("'")
    if not raw_token:
        log.error(
            "SLACK_BOT_TOKEN is not set locally. Source the bot creds first:\n"
            "    set -a && source ~/chalk/github-pinger/.env && set +a"
        )
        sys.exit(1)
    os.environ["SLACK_BOT_TOKEN"] = raw_token

    with open(APP_PATH, encoding="utf-8") as fh:
        app_src = fh.read()

    # Stdlib-only app => a bare debian_slim image is all we need; it caches and
    # reuses across deploys.
    image = Image.debian_slim()

    container = Container(
        image=image,
        name=name,
        port=PORT,  # publishing a port -> public URL + CHALK_CONTAINER_WEB_URL
        cpu="500m",
        memory="512Mi",
        env={
            "PORT": str(PORT),
            "SLACK_CHANNEL": SLACK_CHANNEL,
            "APP_SRC": app_src,
        },
        # Read the bot token from the local environment at deploy time, upsert it
        # as a Chalk secret, and inject it into the pod as SLACK_BOT_TOKEN.
        secrets=[Secret.from_local_env("SLACK_BOT_TOKEN")],
        entrypoint=[
            "python3",
            "-u",
            "-c",
            "import os; exec(compile(os.environ['APP_SRC'], 'app.py', 'exec'))",
        ],
        lifetime="7200s",  # auto-clean after 2h so we don't orphan it
    ).run()

    info = container.info
    web_url = info.web_url if info else None
    log.info("Container '%s' is %s", name, info.status if info else "unknown")
    log.info("Container id: %s", container.id)
    print("\n--- HITL approval service ---")
    print(f"  name:     {name}")
    print(f"  status:   {info.status if info else 'unknown'}")
    print(f"  web url:  {web_url or '(no public URL — port may not be published)'}")
    print(f"  channel:  {SLACK_CHANNEL}")
    print(f"  stop:     uv run python {os.path.relpath(__file__)} --stop {name}")
    print("-----------------------------\n")
    print("Watch #chalkbot-dev for the approval message with Approve / Reject buttons.")
    if web_url:
        print(f"Or open {web_url}/ to see the in-memory request list.")


def stop(name: str) -> None:
    container = Container.from_name(name)
    container.stop()
    log.info("Stopped container '%s'", name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy the HITL Slack-approval container")
    parser.add_argument("--name", default=f"hitl-approval-{secrets.token_hex(3)}", help="Container name")
    parser.add_argument("--stop", metavar="NAME", help="Stop a running container by name and exit")
    args = parser.parse_args()

    if args.stop:
        stop(args.stop)
        return
    deploy(args.name)


if __name__ == "__main__":
    main()
