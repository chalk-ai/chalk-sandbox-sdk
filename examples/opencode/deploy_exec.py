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
    container.exec("bash", "-c", "apt-get update -qq && apt-get install -y -qq curl git", timeout_secs=180)

    log.info("Installing opencode...")
    container.exec("bash", "-c", "curl -fsSL https://opencode.ai/install | bash", timeout_secs=120)


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
    container.exec("bash", "-c", clone_cmd, timeout_secs=120)

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


def print_access_info(container: Container) -> None:
    web_url = container.info.web_url if container.info else None

    print("\n--- OpenCode server ready ---")
    if web_url:
        print(f"  Web UI:  {web_url}")
        print(f"  TUI:     OPENCODE_SERVER_PASSWORD=<password> opencode attach {web_url}")
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
        print_access_info(container)

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
        default="main",
        help="Git branch, tag, or SHA to check out. Default: main",
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
