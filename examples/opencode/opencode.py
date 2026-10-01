#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute>=2.11.1", "python-dotenv"]
# ///

# # OpenCode on Chalk Sandboxes
#
# Run [OpenCode](https://opencode.ai) as a persistent server inside a Chalk sandbox,
# pointed at any GitHub repo. Connect from your local browser or terminal — useful for
# coding sessions that need more compute than your laptop, or for sharing a live
# environment with teammates.
#
# ## Overview
#
# 1. Start sandboxes from a pre-built image with `curl`, `git`, and `opencode` baked in.
# 2. Clone the target GitHub repo into `/root/code`.
# 3. Launch `opencode serve` in the background and poll until it responds.
# 4. Print the Web UI URL and TUI attach command, then hold the sandbox alive.
# All flags can also be set via environment variables or a `.env` file:
#
#     GITHUB_TOKEN              GitHub PAT — required for private repos (gh auth token)
#     GITHUB_REF                Branch/tag/SHA to check out. Default: main
#     OPENCODE_SERVER_PASSWORD  Password to protect the server. Optional.
#     OPENAI_API_KEY            Forwarded into the sandbox. Optional.
#
# This doubles as a sandbox fan-out stress example. It starts 100 sandboxes
# by default, configures the last one as the interactive OpenCode server, and
# stops every sandbox on exit. Pass ``--sandboxes 1`` for the basic workflow.
#
# This example temporarily uses the deprecated `Container` API because its public
# info object exposes the published port's `web_url`; `Sandbox.info` does not. The
# browser and TUI connection flow depends on that URL.
#
# **Privacy warning:** if `~/.config/opencode/opencode.json` exists, this script copies
# it into the remote interactive container. That file may contain provider settings or
# credentials. Review it before running this example.

import argparse
import base64
import logging
import os
import shlex
import time
import uuid
from pathlib import Path

from chalkcompute import Container, Image
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

OPENCODE_PORT = 4096
OPENCODE_BIN = "/root/.opencode/bin/opencode"
DEFAULT_GITHUB_REPO = "chalk-ai/chalk-sandbox-sdk"
APP_SETUP_ALLOWANCE_SECS = 300
SANDBOX_STARTUP_ALLOWANCE_SECS = 60

# ## Base image — curl, git, and opencode pre-installed
#
# Baking dependencies into the image spec lets Chalk build and cache the image
# once. Subsequent sandbox starts reuse the cached image, skipping the
# apt-get and opencode install entirely.

OPENCODE_IMAGE = Image.debian_slim().run_commands(
    "apt-get update -qq && apt-get install -y -qq curl git 2>&1",
    "curl -fsSL https://opencode.ai/install | bash 2>&1",
)


# ## Step 1 — Clone the repo
#
# We shallow-clone a single branch or tag to keep the operation fast. If a token is
# provided it's injected via `GIT_ASKPASS` so the credential doesn't appear in logs.
# We also upload your local `~/.config/opencode/opencode.json` if it exists, carrying
# over model preferences and provider keys without re-configuring inside the sandbox.


def clone_repo(container: Container, repo: str, ref: str, token: str | None) -> None:
    log.info("Cloning %s@%s into /root/code...", repo, ref)
    container.exec("git", "config", "--global", "advice.detachedHead", "false")

    quoted_repo = shlex.quote(repo)
    quoted_ref = shlex.quote(ref)
    if token:
        clone_cmd = (
            "printf '#!/bin/sh\\necho \"$GH_TOKEN\"\\n' > /tmp/git-askpass && "
            "chmod 700 /tmp/git-askpass && "
            f"GIT_ASKPASS=/tmp/git-askpass GIT_TERMINAL_PROMPT=0 git clone --quiet --depth 1 --branch {quoted_ref}"
            f" --no-single-branch https://oauth2@github.com/{quoted_repo}.git /root/code"
        )
    else:
        clone_cmd = (
            f"GIT_TERMINAL_PROMPT=0 git clone --quiet --depth 1 --branch {quoted_ref}"
            f" --no-single-branch https://github.com/{quoted_repo}.git /root/code"
        )
    result = container.exec("bash", "-c", clone_cmd + " 2>&1", timeout_secs=120)
    if result.exit_code != 0:
        raise RuntimeError(
            f"Failed to clone {repo}@{ref} (exit {result.exit_code}).\n"
            f"{result.stdout_text.strip()}\n"
            "Tip: make sure GITHUB_TOKEN is set and has read access to the repo."
        )

    # Upload local opencode config if present — preserves model/provider settings.
    config_path = Path("~/.config/opencode/opencode.json").expanduser()
    if config_path.exists():
        log.warning(
            "Copying %s into the remote container; it may contain provider credentials or configuration.",
            config_path,
        )
        content = base64.b64encode(config_path.read_bytes()).decode()
        container.exec("bash", "-c", "mkdir -p /root/.config/opencode")
        container.exec(
            "bash",
            "-c",
            f"printf %s {shlex.quote(content)} | base64 -d > /root/.config/opencode/opencode.json",
        )


# ## Step 2 — Start the OpenCode server
#
# We run `opencode serve` in the background via `nohup`, redirecting output to
# `/tmp/opencode.log`. We then poll the HTTP endpoint every 2 seconds (up to 2 minutes)
# to confirm it's reachable before printing the URL — so the link we hand back is live.


def start_server(container: Container) -> None:
    log.info("Stopping placeholder HTTP server on port %d...", OPENCODE_PORT)
    container.exec(
        "bash",
        "-c",
        "kill $(cat /tmp/http_server.pid 2>/dev/null) 2>/dev/null; rm -f /tmp/http_server.pid; true",
    )

    log.info("Starting opencode server on port %d...", OPENCODE_PORT)
    container.exec(
        "bash",
        "-c",
        f"nohup {OPENCODE_BIN} serve --hostname=0.0.0.0 --port={OPENCODE_PORT}"
        f" --log-level=DEBUG > /tmp/opencode.log 2>&1 &",
    )

    log.info("Waiting for opencode to be ready...")
    for attempt in range(60):
        result = container.exec(
            "bash",
            "-c",
            f"curl -s http://localhost:{OPENCODE_PORT} > /dev/null 2>&1; echo $?",
        )
        if result.stdout_text.strip() == "0":
            return
        if attempt < 59:
            time.sleep(2)
    raise RuntimeError(
        "opencode server did not become ready after 2 minutes.\n"
        "Check /tmp/opencode.log inside the sandbox for details."
    )


def _print_access_info(container: Container, password: str | None) -> None:
    web_url = container.info.web_url if container.info else None

    print("\n--- OpenCode server ready ---")
    if web_url:
        print(f"  Web UI:  {web_url}")
        password_prefix = f"OPENCODE_SERVER_PASSWORD={password} " if password else ""
        print(f"  TUI:     {password_prefix}opencode attach {web_url}")
    else:
        log.warning(
            "No web_url returned; sandbox is running but port may not be publicly reachable."
        )
        print(f"  Container ID: {container.id}")
    print("-----------------------------\n")


# ## Putting it all together
#
# `main()` starts the sandboxes, runs the setup steps on the last one, then prints
# access info and sleeps until the timeout or Ctrl-C. The `finally` block guarantees
# every sandbox is terminated regardless of how we exit. Each sandbox also has a
# server-side lifetime longer than the requested session. The earliest sandboxes get
# extra time for the remaining fan-out, while the interactive sandbox gets five
# minutes for cloning the repo and starting OpenCode. This bounds resource lifetime
# even if the local process is killed before `finally` runs.


def main(
    sandbox_name: str,
    github_repo: str,
    github_ref: str,
    github_token: str | None,
    password: str | None,
    timeout_secs: int,
    sandbox_count: int,
) -> None:
    env: dict[str, str] = {}
    if password:
        env["OPENCODE_SERVER_PASSWORD"] = password
    if github_token:
        env["GH_TOKEN"] = github_token
    if openai_key := os.environ.get("OPENAI_API_KEY"):
        env["OPENAI_API_KEY"] = openai_key

    containers: list[Container] = []
    try:
        for index in range(sandbox_count):
            name = sandbox_name if sandbox_count == 1 else f"{sandbox_name}-{index + 1}"
            log.info(
                "Starting sandbox %d/%d (%s)...",
                index + 1,
                sandbox_count,
                name,
            )
            remaining_starts = sandbox_count - index
            lifetime_secs = (
                timeout_secs
                + APP_SETUP_ALLOWANCE_SECS
                + remaining_starts * SANDBOX_STARTUP_ALLOWANCE_SECS
            )
            container = Container(
                image=OPENCODE_IMAGE,
                name=name,
                env=env,
                port=OPENCODE_PORT,
                entrypoint=[
                    "bash",
                    "-c",
                    f"python3 -m http.server {OPENCODE_PORT} --bind 0.0.0.0 &>/dev/null & echo $! > /tmp/http_server.pid; tail -f /dev/null",
                ],
                lifetime=f"{lifetime_secs}s",
            )
            containers.append(container)
            container.run()

        container = containers[-1]
        clone_repo(container, github_repo, github_ref, github_token)
        start_server(container)
        _print_access_info(container, password)

        mins = timeout_secs // 60
        log.info("Server running. Press Ctrl-C to stop (timeout: %dm).", mins)
        time.sleep(timeout_secs)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        stopped = 0
        for container in reversed(containers):
            if container.id is None:
                continue
            try:
                container.stop()
                stopped += 1
            except Exception:
                log.exception("Failed to stop container %s", container.id)
        log.info("Stopped %d sandbox(es).", stopped)


def _parse_timeout(value: str) -> int:
    if value.endswith("h"):
        return int(value[:-1]) * 3600
    if value.endswith("m"):
        return int(value[:-1]) * 60
    return int(value) * 3600  # bare number treated as hours


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Launch OpenCode server in Chalk sandboxes"
    )
    parser.add_argument(
        "--sandboxes",
        type=int,
        default=100,
        help="Number of sandboxes to start for the fan-out stress run. Default: 100",
    )
    parser.add_argument(
        "--timeout",
        type=str,
        default="12",
        help="Server lifetime (e.g. 2h, 90m). Bare number treated as hours. Default: 12",
    )
    parser.add_argument(
        "--name",
        type=str,
        default=f"opencode-server-{uuid.uuid4().hex[:8]}",
        help="Sandbox name prefix. Default: opencode-server-<random>",
    )
    parser.add_argument(
        "--password",
        type=str,
        default=os.environ.get("OPENCODE_SERVER_PASSWORD"),
        help="OpenCode server password. Default: $OPENCODE_SERVER_PASSWORD",
    )
    parser.add_argument(
        "--github-repo",
        type=str,
        default=DEFAULT_GITHUB_REPO,
        help=f"GitHub repo to clone (owner/repo). Default: {DEFAULT_GITHUB_REPO}",
    )
    parser.add_argument(
        "--github-ref",
        type=str,
        default=os.environ.get("GITHUB_REF", "main"),
        help="Git branch, tag, or SHA to check out. Default: $GITHUB_REF or main",
    )
    parser.add_argument(
        "--github-token",
        type=str,
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub PAT for private repos. Tip: use $(gh auth token)",
    )

    args = parser.parse_args()
    if args.sandboxes < 1:
        parser.error("--sandboxes must be at least 1")
    main(
        sandbox_name=args.name,
        github_repo=args.github_repo,
        github_ref=args.github_ref,
        github_token=args.github_token,
        password=args.password,
        timeout_secs=_parse_timeout(args.timeout),
        sandbox_count=args.sandboxes,
    )
