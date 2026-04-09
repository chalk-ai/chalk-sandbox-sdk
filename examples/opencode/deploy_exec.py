"""
Launch an OpenCode server in a Chalk container pointed at any GitHub repo.

The container starts from a base Ubuntu image, installs git and opencode, clones your
repo, and starts the opencode server. Connect via the Web UI URL printed at the end.

## Quickstart

    python examples/opencode/deploy_exec.py \\
        --github-repo your-org/your-repo \\
        --github-ref main

## Configuration

All flags can be set via a .env file (loaded automatically) or environment variables:

    GITHUB_TOKEN   GitHub personal access token — required for private repos.
                   Get yours with: gh auth token
    GITHUB_REF     Branch, tag, or SHA to check out. Default: main
    OPENCODE_SERVER_PASSWORD  Password to protect the opencode server. Optional.
    OPENAI_API_KEY OpenAI API key forwarded into the container. Optional —
                   opencode ships with some free models.

## Flags

    --github-repo   GitHub repo to clone, e.g. my-org/my-repo (default: chalk-ai/chalk-sandbox-sdk)
    --github-ref    Branch/tag/SHA to check out (default: $GITHUB_REF or main)
    --github-token  GitHub PAT (default: $GITHUB_TOKEN)
    --password      OpenCode server password (default: $OPENCODE_SERVER_PASSWORD)
    --name          Container name in Chalk (default: opencode-server)
    --timeout       How long to keep the server alive, e.g. 2h, 90m (default: 12h)
"""

import argparse
import logging
import os
import time
from pathlib import Path

from chalkcompute import Container
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

OPENCODE_PORT = 4096
OPENCODE_BIN = "/root/.opencode/bin/opencode"
DEFAULT_GITHUB_REPO = "chalk-ai/chalk-sandbox-sdk"


# ---------------------------------------------------------------------------
# Setup (run inside the container after it starts)
# ---------------------------------------------------------------------------


def install_deps(container: Container) -> None:
    log.info("Installing curl and git...")
    result = container.exec("bash", "-c", "apt-get update -qq && apt-get install -y -qq curl git 2>&1", timeout_secs=180)
    if result.exit_code != 0:
        raise RuntimeError(f"apt-get install failed (exit {result.exit_code}).\n{result.stdout_text.strip()}")

    log.info("Installing opencode...")
    result = container.exec("bash", "-c", "curl -fsSL https://opencode.ai/install | bash 2>&1", timeout_secs=120)
    if result.exit_code != 0:
        raise RuntimeError(f"opencode install failed (exit {result.exit_code}).\n{result.stdout_text.strip()}")


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

    # Copy local opencode config if present.
    config_path = Path("~/.config/opencode/opencode.json").expanduser()
    if config_path.exists():
        log.info("Uploading local opencode config...")
        content = config_path.read_text()
        container.exec("bash", "-c", "mkdir -p /root/.config/opencode")
        container.exec(
            "bash", "-c",
            f"cat > /root/.config/opencode/opencode.json << 'EOFCONFIG'\n{content}\nEOFCONFIG",
        )


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


def start_server(container: Container) -> None:
    log.info("Starting opencode server on port %d...", OPENCODE_PORT)
    container.exec(
        "bash",
        "-c",
        f"nohup {OPENCODE_BIN} serve --hostname=0.0.0.0 --port={OPENCODE_PORT}"
        f" --log-level=DEBUG > /tmp/opencode.log 2>&1 &",
    )

    log.info("Waiting for opencode to be ready...")
    for attempt in range(24):
        result = container.exec(
            "bash", "-c", f"curl -sf http://localhost:{OPENCODE_PORT} > /dev/null 2>&1; echo $?"
        )
        if result.stdout_text.strip() == "0":
            return
        if attempt < 23:
            time.sleep(5)
    raise RuntimeError(
        "opencode server did not become ready after 2 minutes.\n"
        "Check /tmp/opencode.log inside the container for details."
    )


def print_access_info(container: Container, password: str | None) -> None:
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


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

    container = Container(
        image="ubuntu:latest",
        name=container_name,
        env=env,
        port=OPENCODE_PORT,
    ).run()

    try:
        install_deps(container)
        clone_repo(container, github_repo, github_ref, github_token)
        start_server(container)
        print_access_info(container, password)

        mins = timeout_secs // 60
        log.info("Server running. Press Ctrl-C to stop (timeout: %dm).", mins)
        time.sleep(timeout_secs)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        container.stop()
        log.info("Container stopped.")


def _parse_timeout(value: str) -> int:
    """Parse a timeout string: '2h' -> 7200, '90m' -> 5400, '12' -> 43200 (hours)."""
    if value.endswith("h"):
        return int(value[:-1]) * 3600
    if value.endswith("m"):
        return int(value[:-1]) * 60
    return int(value) * 3600  # bare number treated as hours


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Launch OpenCode server in a Chalk container (exec-based setup)")
    parser.add_argument(
        "--timeout",
        type=str,
        default="12",
        help="Server lifetime (e.g. 2h, 90m). Bare number treated as hours. Default: 12",
    )
    parser.add_argument(
        "--name",
        type=str,
        default="opencode-server",
        help="Container name. Default: opencode-server",
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
        help="GitHub PAT for private repos or gh CLI auth. Tip: use $(gh auth token)",
    )

    args = parser.parse_args()
    main(
        container_name=args.name,
        github_repo=args.github_repo,
        github_ref=args.github_ref,
        github_token=args.github_token,
        password=args.password,
        timeout_secs=_parse_timeout(args.timeout),
    )
