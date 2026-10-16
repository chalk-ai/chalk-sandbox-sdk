#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute>=2.12.0"]
# ///

# # Hermes Agent on a Chalk sandbox
#
# Run [Hermes Agent](https://github.com/NousResearch/hermes-agent), Nous Research's
# open-source coding and terminal agent, headless inside a Chalk sandbox. Hermes gets
# a real shell, a filesystem, and a model endpoint. It does not get your laptop, your
# other credentials, or the open internet.
#
# ## Overview
#
# 1. Build an image with Hermes installed from a pinned release. Chalk caches it, so
#    only the first run pays for the install.
# 2. Start a sandbox whose network policy allows exactly two kinds of egress: the
#    model endpoint and, if you pass `--repo`, the git host to clone from.
# 3. Bind the model API key to the model endpoint's host. Hermes sees a placeholder
#    in its environment; the sandbox's egress proxy swaps in the real key only on
#    requests to that host. A prompt-injected agent that prints or exfiltrates its
#    environment leaks nothing usable.
# 4. Write a Hermes config that points at the endpoint, then run one task with
#    `hermes chat --oneshot` and stream its output.
# 5. Show what the agent changed with `git status` and `git diff`, then terminate
#    the sandbox.
#
# ## Model endpoint
#
# Hermes talks to any OpenAI-compatible endpoint. The default is OpenRouter with
# the key in the local `OPENROUTER_API_KEY`. Point `--base-url`, `--model`, and
# `--api-key-env` at another provider, gateway, or a vLLM server you host. The key
# can come from:
#
# - a local environment variable (`--api-key-env`, the default). The SDK stores its
#   value as a secret in your Chalk environment so the sandbox can use it, and that
#   secret remains after this script exits. Delete it from the environment's secrets
#   settings if you do not want it kept.
# - an existing Chalk secret (`--api-key-secret`). Nothing new is stored.
#
# Provide the standard `CHALK_*` credentials in the environment.
#
# ## Safety, privacy, and cost
#
# Hermes runs with `--yolo`, which skips its per-command approval prompts. The
# sandbox is the safety boundary instead: the agent can run anything inside it,
# including destructive commands, but it can reach only the hosts listed above. The
# prompt, the files Hermes reads, and its command output are sent to the model
# provider. Use a repository and prompt you are permitted to share with it.
#
# The sandbox is ephemeral. It is terminated on exit, and its server-side lifetime
# bounds it if this process is killed first. Model usage is billed by your provider.

import argparse
import os
import shlex
import sys
import uuid
from urllib.parse import urlsplit

from chalkcompute import Image, NetworkPolicy, Sandbox, SandboxClient, Secret

# Pin the Hermes release so the image, and Hermes' behavior, are reproducible.
# The installer and the checkout come from the same tag.
HERMES_RELEASE = "v2026.9.24"

WORKDIR = "/root/project"

# Time allowed for scheduling the sandbox and cloning the repository, on top of
# the agent's own time budget, before the server-side lifetime expires.
SETUP_ALLOWANCE_SECS = 300

DEFAULT_PROMPT = (
    "Create a small, standard-library-only Python module roman.py with "
    "to_roman(int) and from_roman(str) for 1-3999, and a test_roman.py that "
    "checks a round trip over the whole range plus invalid inputs using plain "
    "asserts. Run the tests with python and fix any failures. There is no "
    "package index access, so do not install anything. Finish with a one-"
    "paragraph summary of what you built."
)

# ## Step 1 - The image
#
# Hermes' installer clones the release, provisions its own Python with `uv`, and
# links `hermes` onto the PATH. `--non-interactive` skips the setup wizard (this
# example writes the config itself), and the two `--skip` flags leave out the
# Chromium-based browser and desktop-control tools, which a headless coding run
# does not need and which would add several hundred megabytes to the image.

HERMES_IMAGE = Image.debian_slim().run_commands(
    "apt-get update -qq && apt-get install -y -qq --no-install-recommends "
    "ca-certificates curl git xz-utils && rm -rf /var/lib/apt/lists/*",
    f"curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/{HERMES_RELEASE}/scripts/install.sh"
    f" | bash -s -- --branch {HERMES_RELEASE} --non-interactive --skip-browser --skip-computer-use",
    "git config --global user.name hermes && git config --global user.email hermes@example.com",
)


# ## Step 2 - The Hermes config
#
# Hermes reads `~/.hermes/config.yaml`. A named entry under `providers:` is the
# most direct way to describe an OpenAI-compatible endpoint: `key_env` names the
# environment variable that holds the key. Here that variable holds the
# placeholder from step 3, never the key itself.


def hermes_config(base_url: str, model: str, key_env: str) -> str:
    return (
        "model:\n"
        "  provider: endpoint\n"
        f"  default: {model}\n"
        "providers:\n"
        "  endpoint:\n"
        f"    base_url: {base_url}\n"
        f"    key_env: {key_env}\n"
    )


# ## Step 3 - The sandbox
#
# `NetworkPolicy(allowed_hosts=...)` is an allowlist: with a policy in place, the
# sandbox can resolve and reach only the listed hosts. `add_credentials` adds the
# model host to that list, sets the credential's environment variable to a random
# placeholder, and tells the egress proxy to replace that placeholder with the real
# secret when it appears in a request header bound for the model host.


def build_sandbox(
    base_url: str,
    credential: Secret,
    repo: str | None,
    lifetime_secs: int,
) -> Sandbox:
    model_host = urlsplit(base_url).hostname
    if model_host is None:
        raise ValueError(f"--base-url has no host: {base_url!r}")

    extra_hosts: list[str] = []
    if repo is not None:
        repo_host = urlsplit(repo).hostname
        if repo_host is None:
            raise ValueError(f"--repo must be an https URL: {repo!r}")
        extra_hosts.append(repo_host)

    return Sandbox(
        image=HERMES_IMAGE,
        name=f"hermes-{uuid.uuid4().hex[:8]}",
        cpu="2",
        memory="4Gi",
        network_policy=NetworkPolicy(allowed_hosts=extra_hosts),
        lifetime=f"{lifetime_secs}s",
    ).add_credentials(model_host, credential)


# ## Step 4 - Prepare the project and run Hermes
#
# Without `--repo`, the project is an empty git repository, so the final diff
# shows everything Hermes created. `--oneshot` answers the prompt and exits;
# `--yolo` lets it run shell commands without asking. The prompt travels as an
# argument vector, not through a shell, so it needs no quoting.


def prepare_project(sandbox: Sandbox, repo: str | None) -> None:
    if repo is None:
        command = f"mkdir -p {WORKDIR} && cd {WORKDIR} && git init -q && git commit -q --allow-empty -m init"
    else:
        command = f"GIT_TERMINAL_PROMPT=0 git clone -q --depth 1 {shlex.quote(repo)} {WORKDIR}"
    result = sandbox.exec("bash", "-c", command + " 2>&1", timeout_secs=300)
    if result.exit_code != 0:
        raise RuntimeError(f"Project setup failed:\n{result.stdout_text}")


def run_hermes(sandbox: Sandbox, prompt: str, timeout_secs: int) -> int:
    exit_code = 1
    for event in sandbox.exec_stream(
        "hermes",
        "chat",
        "--oneshot",
        "--yolo",
        "--query",
        prompt,
        workdir=WORKDIR,
        timeout_secs=timeout_secs,
        # Keeps bytecode caches from the agent's test runs out of the final diff.
        env={"PYTHONDONTWRITEBYTECODE": "1"},
    ):
        if event.stdout:
            print(event.stdout, end="", flush=True)
        if event.stderr:
            print(event.stderr, end="", file=sys.stderr, flush=True)
        if event.is_error:
            print(f"\nexec error: {event.error_message}", file=sys.stderr)
        if event.exit_code is not None:
            exit_code = event.exit_code
    return exit_code


# ## Step 5 - Show the result
#
# `git add -N` marks new files as intended-to-add so `git diff` includes them.


def show_changes(sandbox: Sandbox) -> None:
    result = sandbox.exec(
        "bash",
        "-c",
        "git add -N . && git status --short && echo && git --no-pager diff",
        workdir=WORKDIR,
    )
    print("\n--- Changes made by Hermes ---")
    print(result.stdout_text.strip() or "(none)")


# ## Putting it together
#
# The sandbox handle is constructed before it starts, so the `finally` block can
# terminate a sandbox even if startup or the agent run fails partway.


def main(args: argparse.Namespace) -> int:
    if args.api_key_secret is not None:
        credential = Secret.from_chalk_env(args.api_key_secret)
        key_env = args.api_key_secret
    else:
        if not os.environ.get(args.api_key_env):
            sys.exit(f"Set {args.api_key_env} or pass --api-key-secret.")
        credential = Secret.from_local_env(args.api_key_env)
        key_env = args.api_key_env

    sandbox = build_sandbox(
        base_url=args.base_url,
        credential=credential,
        repo=args.repo,
        lifetime_secs=args.timeout + SETUP_ALLOWANCE_SECS,
    )
    client = SandboxClient.from_env()
    try:
        print("Starting sandbox (the first run builds the Hermes image)...")
        client.run(sandbox)
        prepare_project(sandbox, args.repo)

        config = hermes_config(args.base_url, args.model, key_env)
        sandbox.exec(
            "bash",
            "-c",
            f"printf %s {shlex.quote(config)} > ~/.hermes/config.yaml",
        )

        print(f"Running Hermes with {args.model}...\n")
        exit_code = run_hermes(sandbox, args.prompt, args.timeout)
        show_changes(sandbox)
        return exit_code
    finally:
        try:
            sandbox_id = sandbox.id
            if sandbox_id is not None:
                sandbox.terminate()
                print(f"Terminated sandbox {sandbox_id}.")
        finally:
            client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run Hermes Agent on one task inside a Chalk sandbox."
    )
    parser.add_argument(
        "--prompt",
        default=DEFAULT_PROMPT,
        help="Task for Hermes. Default: build and test a small Roman numeral module.",
    )
    parser.add_argument(
        "--repo",
        help="Public git repository (https URL) to clone as the working directory. "
        "Its host is added to the sandbox's network allowlist.",
    )
    parser.add_argument(
        "--base-url",
        default="https://openrouter.ai/api/v1",
        help="OpenAI-compatible model endpoint. Default: OpenRouter.",
    )
    parser.add_argument(
        "--model",
        default="anthropic/claude-sonnet-5.5",
        help="Model name as the endpoint expects it. Default: anthropic/claude-sonnet-5.5",
    )
    key_source = parser.add_mutually_exclusive_group()
    key_source.add_argument(
        "--api-key-env",
        default="OPENROUTER_API_KEY",
        help="Local environment variable holding the endpoint's API key. "
        "Default: OPENROUTER_API_KEY",
    )
    key_source.add_argument(
        "--api-key-secret",
        help="Existing Chalk secret holding the endpoint's API key, used instead "
        "of a local environment variable.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=900,
        help="Seconds Hermes may run before it is stopped. Default: 900",
    )
    sys.exit(main(parser.parse_args()))
