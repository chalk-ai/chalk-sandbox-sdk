#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["chalkcompute>=2.13.4", "pyarrow"]
# ///

# # Review GitHub repositories with a coding agent, from SQL
#
# Deploy a Chalk function, `review_repository(repo, prompt)`, that clones a GitHub
# repository and runs the [pi](https://pi.dev) coding agent in it. The agent explores
# the checkout with read-only tools for at most 30 seconds, and the function returns
# its answer. Because the function is registered in the environment's catalog, any
# Chalk SQL query can call it, one call per row:
#
#     select catalog_call('function.review_repository',
#                         'chalk-ai/chalk-harbor',
#                         'what is the content of this repository?')
#
# A table of repositories therefore becomes a table of reviews, and Chalk spreads
# the calls across the function's replicas, reviewing each batch's rows in parallel.
#
# ## Overview
#
# 1. Build an image with Node.js, `pi`, `git`, `ripgrep`, and `fd` installed.
# 2. Deploy `review_repository`. Each call shallow-clones the repository into a
#    temporary directory and runs one non-interactive `pi` session in it.
# 3. Stream pi's JSON events, and stop the session once its 30-second budget runs out.
#    The answer is the agent's last assistant message, or whatever it had written so
#    far if the budget ran out first.
# 4. Run the `catalog_call` query through Chalk SQL and print each repository's answer.
#
# ## Model access
#
# pi talks to Chalk's AI router, an OpenAI-compatible endpoint under the
# environment's API server, so no provider key is needed. The router credential comes
# from the environment secret `CHALK_ROUTER_API_KEY`. Create it first as a Chalk
# secret holding a router API key. Every completion is billed through the
# environment's router configuration.
#
# ## Making the function visible to SQL
#
# Chalk SQL resolves `catalog_call` names from the function catalog that the
# environment's engine loaded when it was last deployed. After this function's first
# deploy, or after changing its signature, re-activate the environment's current
# deployment (`chalk deployment activate --deployment <id>`) so the engine reloads
# the catalog. Until then the query fails with `Service 'review_repository' is not
# configured`.
#
# ## Private repositories
#
# Public repositories clone without credentials. For private ones, set `GITHUB_TOKEN`
# locally before running this script. The SDK then stores it as a secret owned by
# this function and injects it into the function's replicas. Prefer a fine-grained
# token with read-only `Contents` access to only the repositories you plan to review.
# Anyone who can call the function can read those repositories through it.
#
# ## Safety
#
# Repository content is untrusted. The agent receives only pi's read-only tools
# (`read`, `grep`, `find`, `ls`), with no shell and no editing. Project-local pi
# configuration and extensions in the checkout are ignored. Files such as `AGENTS.md`
# can still steer the model's answer, so treat answers as untrusted text too. Prompts
# and file contents are sent to the model provider behind the router.
#
# ## Lifecycle
#
# The function deployment is intentionally persistent: it stays registered after this
# script exits so that later SQL queries can call it, and it keeps one replica running
# (billable capacity) until you delete it. Delete it with
# `chalkcompute.RemoteFunction.from_name("review_repository").delete()`. Each call's
# checkout and pi state live in a temporary directory deleted when the call returns.

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from base64 import b64encode
from pathlib import Path

import chalkcompute
from chalkcompute import Image, Secret

SESSION_SECONDS = 30
CLONE_TIMEOUT_SECONDS = 120
MODEL = "anthropic/claude-haiku-5-5"
NODE_VERSION = "24.21.0"
PI_VERSION = "1.1.0"
PI_TOOLS = "read,grep,find,ls"
# pi cannot see the clock, so the prompt states the budget. Without it the agent tends
# to keep exploring and is cut off before it writes an answer.
TIME_BUDGET_PROMPT = (
    f"You have a hard limit of {SESSION_SECONDS} seconds for this whole session, after"
    " which it is stopped and only text you have already written is kept. Use at most"
    " five tool calls, then write your final answer."
)
DEFAULT_REPO = "chalk-ai/chalk-harbor"
DEFAULT_PROMPT = "what is the content of this repository?"

# `owner/name` only, so a caller cannot point git at another host, a local path, or
# an option-shaped argument.
_REPO_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")

# ## Image: Node.js, pi, and the search tools pi's read-only tools shell out to
#
# pi's `grep` and `find` tools run `rg` and `fd`. Debian names the fd binary
# `fdfind`, so it is linked under the name pi looks for. pi is pinned, and npm
# install scripts are skipped, so every replica runs the same agent build. Chalk
# caches the image after the first build.

REVIEW_IMAGE = Image.debian_slim("3.12").run_commands(
    "apt-get update -qq && apt-get install -y -qq --no-install-recommends"
    " ca-certificates curl git ripgrep fd-find xz-utils"
    " && ln -s /usr/bin/fdfind /usr/local/bin/fd && rm -rf /var/lib/apt/lists/*",
    f"curl -fsSL https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-x64.tar.xz"
    " | tar -xJ -C /usr/local --strip-components=1",
    f"npm install -g --ignore-scripts @earendil-works/pi-coding-agent@{PI_VERSION}",
)

_SECRETS = [Secret.from_chalk_env("CHALK_ROUTER_API_KEY")]
if os.environ.get("GITHUB_TOKEN"):
    _SECRETS.append(Secret.from_local_env("GITHUB_TOKEN"))

# The router URL is fixed at deploy time from the deploying environment's API
# server, so the replicas call the same environment the function is registered in.
_ROUTER_ENV = {
    "CHALK_ROUTER_BASE_URL": os.environ.get(
        "CHALK_API_SERVER", "https://api.chalk.ai"
    ).rstrip("/")
    + "/v1/router",
}


# ## The function
#
# Chalk SQL sends a query's rows to the function in batches. With batching enabled
# (`max_batching_size`), the function is still written for one row, and each row of a
# batch runs on its own thread, so a batch of reviews runs in parallel rather than one
# after another. Each review uses its own temporary directory for the checkout and
# pi's state, so concurrent reviews never see each other's files.
#
# A batch succeeds or fails as a whole. One unreachable repository must not discard
# the other reviews in its batch, so a failed review returns its reason as the
# answer, prefixed with `review failed:`, instead of raising.


@chalkcompute.function(
    image=REVIEW_IMAGE,
    cpu="4",
    memory="8Gi",
    secrets=_SECRETS,
    env=_ROUTER_ENV,
    max_batching_size=16,
    max_buffer_duration=200,
)
def review_repository(repo: str, prompt: str) -> str:
    """Clone ``repo`` (``owner/name``) and return pi's answer to ``prompt``."""
    try:
        if not _REPO_PATTERN.fullmatch(repo):
            raise ValueError(f"repo must look like 'owner/name', got {repo!r}")
        if not prompt.strip():
            raise ValueError("prompt cannot be empty")
        with tempfile.TemporaryDirectory(prefix="review-") as workdir:
            checkout = Path(workdir) / "repo"
            _clone(repo, checkout)
            agent_dir = Path(workdir) / "pi-agent"
            _write_pi_config(agent_dir)
            return _run_pi_session(checkout, agent_dir, prompt)
    except Exception as exc:
        return f"review failed: {exc}"


# ## Step 1: Clone
#
# A depth-1 clone keeps large histories off the critical path. The token travels in a
# git config entry passed through the environment, so it never appears in a process
# argument list or in the checkout's `.git/config`.


def _clone(repo: str, checkout: Path) -> None:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if token := os.environ.get("GITHUB_TOKEN"):
        basic = b64encode(f"x-access-token:{token}".encode()).decode()
        env |= {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        }
    result = subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--depth",
            "1",
            f"https://github.com/{repo}.git",
            str(checkout),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=CLONE_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        hint = (
            "" if "GITHUB_TOKEN" in os.environ else " (private repos need GITHUB_TOKEN)"
        )
        raise RuntimeError(f"git clone of {repo} failed{hint}: {result.stderr.strip()}")


# ## Step 2: Point pi at the AI router
#
# pi reads custom providers from `models.json` in its agent directory. The key and
# header values are `$NAME` references, which pi resolves from its environment at
# request time, so the secret is never written to disk.


def _write_pi_config(agent_dir: Path) -> None:
    agent_dir.mkdir()
    models = {
        "providers": {
            "chalk": {
                "baseUrl": os.environ["CHALK_ROUTER_BASE_URL"],
                "api": "openai-completions",
                "apiKey": "$CHALK_ROUTER_API_KEY",
                "headers": {"X-Chalk-Env-Id": "$CHALK_ENVIRONMENT_ID"},
                "models": [{"id": MODEL}],
            }
        }
    }
    (agent_dir / "models.json").write_text(json.dumps(models))


# ## Step 3: Run one bounded pi session
#
# `pi -p --mode json` runs a single prompt and writes one JSON event per line. A
# reader thread keeps the latest assistant text: `text_delta` events build up the
# message being written, and `message_end` replaces it with the complete message.
# The main thread waits for at most `SESSION_SECONDS`, then stops pi. A stopped
# session still returns the text the agent had produced, marked as cut off.
#
# The flags keep the session read-only and self-contained: `--tools` allows only the
# read-only tools, `--no-approve` ignores any `.pi/` configuration or extensions the
# repository ships, `--no-session` skips writing a transcript, and `PI_OFFLINE` stops
# pi from downloading tools or catalog updates at startup. `--append-system-prompt`
# tells the agent about the time budget.


def _run_pi_session(checkout: Path, agent_dir: Path, prompt: str) -> str:
    pi = shutil.which("pi")
    if pi is None:
        raise RuntimeError("pi is not installed in this image")
    command = [
        pi,
        "--print",
        "--mode", "json",
        "--no-session",
        "--no-approve",
        "--tools", PI_TOOLS,
        "--model", f"chalk/{MODEL}",
        "--append-system-prompt", TIME_BUDGET_PROMPT,
        prompt,
    ]  # fmt: skip
    env = {
        **os.environ,
        "PI_CODING_AGENT_DIR": str(agent_dir),
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
    }
    process = subprocess.Popen(
        command,
        cwd=checkout,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    transcript = _Transcript()
    stderr_tail: list[bytes] = []
    readers = [
        threading.Thread(
            target=transcript.consume, args=(process.stdout,), daemon=True
        ),
        threading.Thread(
            target=_drain, args=(process.stderr, stderr_tail), daemon=True
        ),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        process.wait(timeout=SESSION_SECONDS)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    for reader in readers:
        reader.join(timeout=5)

    answer = transcript.answer().strip()
    if timed_out:
        note = f"[pi session stopped after {SESSION_SECONDS}s]"
        return f"{answer}\n\n{note}" if answer else note
    if process.returncode != 0 or transcript.error:
        detail = transcript.error or b"".join(stderr_tail).decode(errors="replace")
        raise RuntimeError(
            f"pi exited with status {process.returncode}: {detail.strip()[-2000:]}"
        )
    return answer


class _Transcript:
    """The latest assistant text from pi's JSON event stream."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._completed = ""
        self._partial = ""
        self.error = ""

    def consume(self, stream) -> None:  # noqa: ANN001 - a binary pipe
        # pi frames records on LF only; Unicode line separators may occur inside strings.
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            with self._lock:
                self._apply(event)

    def _apply(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "message_update":
            update = event.get("assistantMessageEvent") or {}
            if update.get("type") == "text_delta":
                self._partial += update.get("delta", "")
        elif kind == "message_end":
            message = event.get("message") or {}
            if message.get("role") != "assistant":
                return
            self._partial = ""
            text = "".join(
                block.get("text", "")
                for block in message.get("content") or []
                if block.get("type") == "text"
            )
            if text.strip():
                self._completed = text
            if message.get("stopReason") == "error":
                self.error = message.get("errorMessage") or "model request failed"

    def answer(self) -> str:
        with self._lock:
            return self._partial if self._partial.strip() else self._completed


def _drain(stream, tail: list[bytes]) -> None:  # noqa: ANN001 - a binary pipe
    """Keep pi's stderr flowing, retaining only the end for error messages."""
    for chunk in stream:
        tail.append(chunk)
        del tail[:-50]


# ## Calling the function from Chalk SQL
#
# `catalog_call` takes the function's qualified catalog name, `function.<name>`,
# followed by the function's arguments in parameter order. Its only named arguments
# are call options such as `timeout` and `on_error`. A single repository uses the
# query from the introduction. Several repositories become a `VALUES` table with one call
# per row, which is the shape of a bulk review.


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def review_query(repos: list[str], prompt: str) -> str:
    call = "catalog_call('function.review_repository', {repo}, {prompt})"
    if len(repos) == 1:
        return (
            "select "
            + call.format(repo=_sql_literal(repos[0]), prompt=_sql_literal(prompt))
            + " as answer"
        )
    rows = ", ".join(f"({_sql_literal(repo)})" for repo in repos)
    return (
        "select repo, "
        + call.format(repo="repo", prompt=_sql_literal(prompt))
        + f" as answer from (values {rows}) as repos(repo)"
    )


def main(repos: list[str], prompt: str) -> None:
    for repo in repos:
        if not _REPO_PATTERN.fullmatch(repo):
            raise SystemExit(f"--repo must look like 'owner/name', got {repo!r}")

    # The decorator started deploying when this module was imported. A cold image
    # build plus first replica start can take several minutes.
    print("Waiting for review_repository to deploy...")
    review_repository.wait_ready(timeout=900)

    sql = review_query(repos, prompt)
    print(f"\n{sql}\n")
    started = time.monotonic()
    with chalkcompute.SQLClient() as client:
        table = client.query(sql, timeout=600).to_arrow()
    print(f"{table.num_rows} row(s) in {time.monotonic() - started:.1f}s\n")
    for row in table.to_pylist():
        print(f"=== {row.get('repo', repos[0])} ===")
        print(row["answer"], end="\n\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Deploy review_repository and call it through Chalk SQL."
    )
    parser.add_argument(
        "--repo",
        action="append",
        help=f"GitHub repository as owner/name; repeat to review several. Default: {DEFAULT_REPO}",
    )
    parser.add_argument(
        "--prompt", default=DEFAULT_PROMPT, help="Question for the agent."
    )
    args = parser.parse_args()
    main(repos=args.repo or [DEFAULT_REPO], prompt=args.prompt)
