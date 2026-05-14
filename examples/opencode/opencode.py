#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute", "python-dotenv"]
# ///

# # OpenCode on Chalk Containers
#
# Run [OpenCode](https://opencode.ai) as a persistent server inside a Chalk container,
# pointed at any GitHub repo. Connect from your local browser or terminal — useful for
# coding sessions that need more compute than your laptop, or for sharing a live
# environment with teammates.
#
# ## Overview
#
# 1. Start a container from a pre-built image with `curl`, `git`, and `opencode` baked in.
# 2. Clone the target GitHub repo into `/root/code`.
# 3. Launch `opencode serve` in the background and poll until it responds.
# 4. Print the Web UI URL and TUI attach command, then hold the container alive.
#
# Run it:
#
#     python examples/opencode/opencode.py \
#         --github-repo your-org/your-repo \
#         --github-ref main
#
# All flags can also be set via environment variables or a `.env` file:
#
#     GITHUB_TOKEN              GitHub PAT — required for private repos (gh auth token)
#     GITHUB_REF                Branch/tag/SHA to check out. Default: main
#     OPENCODE_SERVER_PASSWORD  Password to protect the server. Optional.
#     OPENAI_API_KEY            Forwarded into the container. Optional.

import argparse
import logging
import os
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

# ## Base image — curl, git, and opencode pre-installed
#
# Baking dependencies into the image spec lets Chalk build and cache the image
# once. Subsequent `Container.run()` calls reuse the cached image, skipping the
# apt-get and opencode install entirely.

OPENCODE_IMAGE = (
    Image.debian_slim()
    .run_commands(
        "apt-get update -qq && apt-get install -y -qq curl git 2>&1",
        "curl -fsSL https://opencode.ai/install | bash 2>&1",
    )
)


# ## Step 1 — Clone the repo
#
# We shallow-clone a single branch or tag to keep the operation fast. If a token is
# provided it's injected via `GIT_ASKPASS` so the credential doesn't appear in logs.
# We also upload your local `~/.config/opencode/opencode.json` if it exists, carrying
# over model preferences and provider keys without re-configuring inside the container.

def clone_repo(container: Container, repo: str, ref: str, token: str | None) -> None:
    log.info("Cloning %s@%s into /root/code...", repo, ref)
    container.exec("git", "config", "--global", "advice.detachedHead", "false")

    if token:
        clone_cmd = (
            f"GIT_ASKPASS=echo git clone --quiet --depth 1 --branch {ref}"
            f" --no-single-branch https://oauth2:{token}@github.com/{repo}.git /root/code"
        )
    else:
        clone_cmd = (
            f"GIT_TERMINAL_PROMPT=0 git clone --quiet --depth 1 --branch {ref}"
            f" --no-single-branch https://github.com/{repo}.git /root/code"
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
        log.info("Uploading local opencode config...")
        content = config_path.read_text()
        container.exec("bash", "-c", "mkdir -p /root/.config/opencode")
        container.exec(
            "bash", "-c",
            f"cat > /root/.config/opencode/opencode.json << 'EOFCONFIG'\n{content}\nEOFCONFIG",
        )


# ## Step 2 — Start the OpenCode server
#
# We run `opencode serve` in the background via `nohup`, redirecting output to
# `/tmp/opencode.log`. We then poll the HTTP endpoint every 2 seconds (up to 2 minutes)
# to confirm it's reachable before printing the URL — so the link we hand back is live.

def start_server(container: Container) -> None:
    log.info("Stopping placeholder HTTP server on port %d...", OPENCODE_PORT)
    container.exec(
        "bash", "-c",
        "kill $(cat /tmp/http_server.pid 2>/dev/null) 2>/dev/null; rm -f /tmp/http_server.pid; true",
    )

    log.info("Starting opencode server on port %d...", OPENCODE_PORT)
    container.exec(
        "bash", "-c",
        f"nohup {OPENCODE_BIN} serve --hostname=0.0.0.0 --port={OPENCODE_PORT}"
        f" --log-level=DEBUG > /tmp/opencode.log 2>&1 &",
    )

    log.info("Waiting for opencode to be ready...")
    for attempt in range(60):
        result = container.exec(
            "bash", "-c",
            f"curl -s http://localhost:{OPENCODE_PORT} > /dev/null 2>&1; echo $?",
        )
        if result.stdout_text.strip() == "0":
            return
        if attempt < 59:
            time.sleep(2)
    raise RuntimeError(
        "opencode server did not become ready after 2 minutes.\n"
        "Check /tmp/opencode.log inside the container for details."
    )


def _print_access_info(container: Container, password: str | None) -> None:
    web_url = container.info.web_url if container.info else None

    print("\n--- OpenCode server ready ---")
    if web_url:
        print(f"  Web UI:  {web_url}")
        password_prefix = f"OPENCODE_SERVER_PASSWORD={password} " if password else ""
        print(f"  TUI:     {password_prefix}opencode attach {web_url}")
    else:
        log.warning("No web_url returned; container is running but port may not be publicly reachable.")
        print(f"  Container ID: {container.id}")
    print("-----------------------------\n")


# ## Putting it all together
#
# `main()` starts the container, runs the three setup steps in order, then prints
# access info and sleeps until the timeout or Ctrl-C. The `finally` block guarantees
# the container is stopped regardless of how we exit — no orphaned containers.

def main(
    container_name: str,
    github_repo: str,
    github_ref: str,
    github_token: str | None,
    password: str | None,
    timeout_secs: int,
) -> None:
    env: dict[str, str] = {}
    if password:
        env["OPENCODE_SERVER_PASSWORD"] = password
    if github_token:
        env["GH_TOKEN"] = github_token
    if openai_key := os.environ.get("OPENAI_API_KEY"):
        env["OPENAI_API_KEY"] = openai_key

    for _ in range(100):
        container = Container(
            image=OPENCODE_IMAGE,
            name=container_name,
            env=env,
            port=OPENCODE_PORT,
            entrypoint=[
                "bash", "-c",
                f"python3 -m http.server {OPENCODE_PORT} --bind 0.0.0.0 &>/dev/null & echo $! > /tmp/http_server.pid; tail -f /dev/null",
            ],
        ).run()

    try:
        clone_repo(container, github_repo, github_ref, github_token)
        start_server(container)
        _print_access_info(container, password)

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
    return int(value) * 3600  # bare number treated as hours


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Launch OpenCode server in a Chalk container")
    parser.add_argument("--timeout", type=str, default="12",
                        help="Server lifetime (e.g. 2h, 90m). Bare number treated as hours. Default: 12")
    parser.add_argument("--name", type=str, default=f"opencode-server-{uuid.uuid4().hex[:8]}",
                        help="Container name. Default: opencode-server-<random>")
    parser.add_argument("--password", type=str, default=os.environ.get("OPENCODE_SERVER_PASSWORD"),
                        help="OpenCode server password. Default: $OPENCODE_SERVER_PASSWORD")
    parser.add_argument("--github-repo", type=str, default=DEFAULT_GITHUB_REPO,
                        help=f"GitHub repo to clone (owner/repo). Default: {DEFAULT_GITHUB_REPO}")
    parser.add_argument("--github-ref", type=str, default=os.environ.get("GITHUB_REF", "main"),
                        help="Git branch, tag, or SHA to check out. Default: $GITHUB_REF or main")
    parser.add_argument("--github-token", type=str, default=os.environ.get("GITHUB_TOKEN"),
                        help="GitHub PAT for private repos. Tip: use $(gh auth token)")

    args = parser.parse_args()
    main(
        container_name=args.name,
        github_repo=args.github_repo,
        github_ref=args.github_ref,
        github_token=args.github_token,
        password=args.password,
        timeout_secs=_parse_timeout(args.timeout),
    )
