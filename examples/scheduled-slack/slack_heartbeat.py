#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["chalkcompute>=2.11.1"]
# ///
# # Scheduled Slack heartbeat
#
# Deploy a remote function that fires every minute and posts the current
# UTC time to a Slack channel. The platform invokes the function on the
# schedule — there is no caller and no return value, so the body just runs
# its side effect.
#
# The deployment intentionally persists after this script exits; otherwise the
# platform-managed schedule could not continue posting heartbeats.
#
# This example shows three things layered together:
#
#   1. `schedule="1m"` turns an ordinary `@chalkcompute.function` into a
#      cron-style job. Scheduled functions take no inputs and must not be
#      generators (see `_normalize_schedule` in `chalkcompute/_function.py`).
#   2. `Secret.from_local_env("SLACK_BOT_TOKEN")` reads the token from the
#      *local* environment at deploy time, upserts it as a Chalk secret in
#      the environment, and injects it into the pod as `SLACK_BOT_TOKEN`.
#      The secret value never appears in the function source.
#   3. The function imports `httpx` at call time and POSTs to Slack's
#      `chat.postMessage` API. The pip dependency lives on the deploy
#      image, not in the local script.

from __future__ import annotations

import logging
import os

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# ## The channel and message
#
# Pinned at module scope so the function body stays small. `SLACK_CHANNEL`
# is plumbed through as a plain env var (not a secret) because the channel
# name is not sensitive.

SLACK_CHANNEL = os.environ["SLACK_CHANNEL"]


# ## The scheduled function
#
# The decorator does three jobs at once: it builds a deploy image with
# `httpx`, wires the local `SLACK_BOT_TOKEN` in as a secret env var, and
# registers the function with `schedule="1m"` so the platform will invoke
# it once a minute.
#
# The body imports `httpx` lazily — the import only needs to resolve in
# the *deployed* pod, not in the script that defines the function.


@chalkcompute.function(
    image=chalkcompute.Image.debian_slim().pip_install(["httpx"]),
    secrets=[chalkcompute.Secret.from_local_env("SLACK_BOT_TOKEN")],
    env={"SLACK_CHANNEL": SLACK_CHANNEL},
    schedule="1m",
)
def slack_heartbeat() -> None:
    """Post the current UTC time to Slack. Invoked on a 1-minute schedule."""
    import os
    from datetime import datetime, timezone

    import httpx

    token = os.environ["SLACK_BOT_TOKEN"]
    channel = os.environ["SLACK_CHANNEL"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    resp = httpx.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {token}"},
        json={"channel": channel, "text": f"heartbeat: current time is {now}"},
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack API error: {data}")
    log.info("posted heartbeat to %s at %s", channel, now)


# ## Putting it all together
#
# `@chalkcompute.function(...)` starts deployment in the background. Reading
# `version_info` below waits for the image build and registration, after which
# the schedule is live. A definition-only script also waits for its background
# deployments before the process exits.

if __name__ == "__main__":
    info = slack_heartbeat.version_info
    assert info is not None
    log.info(
        "deployed: name=%s version=%d id=%s",
        info.function_name,
        info.version,
        info.id,
    )
    log.info(
        "the platform will invoke this function every minute — watch %s", SLACK_CHANNEL
    )
