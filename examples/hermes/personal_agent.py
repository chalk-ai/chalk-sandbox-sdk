#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute>=2.13.0"]
# ///

# # Hermes as a personal assistant, in a Chalk sandbox
#
# Give [Hermes Agent](https://github.com/NousResearch/hermes-agent) a week of your
# calendar and a few notes about your household. It finds the conflicts and the times
# nobody is free to pick up a child, proposes who covers what, searches the web for
# childcare near you, and hands back a plan, a revised calendar to import, and draft
# messages for you to send.
#
# The interesting part is what Hermes cannot do. It runs in a sandbox with:
#
# - **only the files you hand it**: a calendar export (`.ics`) and a notes file. It has
#   no login to your calendar, email, or messages, so it can propose changes and draft
#   messages but cannot make or send them.
# - **only the network it needs**: the model endpoint and Hermes' built-in keyless web
#   search services. It cannot reach anything else.
# - **no real API key**: the model key is swapped in at the sandbox's network edge, as
#   in `hermes.py`.
#
# ## Overview
#
# 1. Start a sandbox from the same pinned Hermes image as `hermes.py`, allowing egress
#    to the model host and the web search services.
# 2. Upload the calendar and notes. By default these are `sample_week.ics` and
#    `household.md` next to this script: a fictional family's very busy week.
# 3. Run one Hermes task (`PERSONAL_ASSISTANT_PROMPT`) and stream its progress.
# 4. Download everything Hermes wrote to `out/` into a local directory, then terminate
#    the sandbox.
#
# Expect `plan.md`, `childcare_options.md`, and `proposed_week.ics`. The run takes a few
# minutes; the first run also builds the Hermes image.
#
# ## Model endpoint and key
#
# Same as `hermes.py`: any OpenAI-compatible endpoint, OpenRouter by default. Pass
# `--api-key-env` (the default, `OPENROUTER_API_KEY`) to read the key from your shell,
# which stores it as a secret in your Chalk environment that remains after this script
# exits, or `--api-key-secret` to use an existing Chalk secret. Provide the standard
# `CHALK_*` credentials in the environment.
#
# ## Privacy
#
# Your calendar and notes are sent to the model provider. Hermes' search queries, which
# may include your neighborhood, go to the keyless search services below without any
# account or key. Use real data only if you are comfortable sharing it with them. The
# sandbox is terminated on exit, and its server-side lifetime bounds it if this process
# is killed first. The downloaded output directory is yours to keep or delete.

import argparse
import os
import shlex
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from chalkcompute import NetworkPolicy, Sandbox, SandboxClient, Secret

# `hermes.py` sits next to this script and owns the pinned image and the provider
# config. Importing them keeps one Hermes version and one cached image for both examples.
from hermes import HERMES_IMAGE, hermes_config

HERE = Path(__file__).resolve().parent
HOUSEHOLD_DIR = "/root/household"
OUT_DIR = f"{HOUSEHOLD_DIR}/out"
SETUP_ALLOWANCE_SECS = 300

# Hermes' keyless web tier: with no search API key configured, `web_search` and
# `web_extract` go to these services' free endpoints and fail over between them. The
# services fetch pages on Hermes' behalf, so the sandbox never needs to reach the
# sites in the results directly.
KEYLESS_WEB_HOSTS = [
    "mcp.exa.ai",
    "search.parallel.ai",
    "api.firecrawl.dev",
    "api.keenable.ai",
]

PERSONAL_ASSISTANT_PROMPT = f"""\
You are a personal assistant for the household described in {HOUSEHOLD_DIR}/household.md.
Their calendar for the coming week is {HOUSEHOLD_DIR}/calendar.ics.

1. Read both files. List every scheduling conflict, and every time a child would be
   without an adult (school pickups, appointments, activities), given where each adult is.
2. Propose a resolution for each one: who covers it, what moves, or that outside
   childcare is needed. Respect the constraints in the notes.
3. For the gaps that need outside childcare, use web search to find 3 to 5 real options
   near the family: after-school programs, babysitting services, or agencies. Prefer ones
   close to the school. For each, give the name, URL, what it offers, hours and price if
   published, and how to book. Include only options you found in search results, and say
   when a detail is not published.
4. Write {OUT_DIR}/plan.md: a day-by-day plan for the week, each conflict and how it is
   resolved, and short draft messages the family can send (for example to a doctor's
   office, a manager, and the top childcare option). Do not contact anyone.
5. Write {OUT_DIR}/childcare_options.md with the options from step 3 as a table, citing
   source URLs.
6. Write {OUT_DIR}/proposed_week.ics: the calendar with your proposed changes applied and
   holds added for childcare, as valid iCalendar using the same TZID. Check that it parses
   with a short standard-library Python script.

Finish with a five-line summary of the week.
"""


# ## Step 1 - The sandbox
#
# The allowlist is the keyless search hosts; `add_credentials` adds the model host and
# binds the key to it. Nothing else is reachable, including calendar and email APIs.


def build_sandbox(base_url: str, credential: Secret, lifetime_secs: int) -> Sandbox:
    model_host = urlsplit(base_url).hostname
    if model_host is None:
        raise ValueError(f"--base-url has no host: {base_url!r}")
    return Sandbox(
        image=HERMES_IMAGE,
        name=f"hermes-assistant-{uuid.uuid4().hex[:8]}",
        cpu="2",
        memory="4Gi",
        network_policy=NetworkPolicy(allowed_hosts=KEYLESS_WEB_HOSTS),
        lifetime=f"{lifetime_secs}s",
    ).add_credentials(model_host, credential)


# ## Step 2 - Hand Hermes the inputs
#
# `sandbox.fs` copies files in and out of a running sandbox. The calendar is always
# stored as `calendar.ics` so the prompt does not depend on your file name.


def upload_inputs(sandbox: Sandbox, calendar: Path, household: Path) -> None:
    sandbox.exec("mkdir", "-p", OUT_DIR)
    sandbox.fs.upload_file(calendar, f"{HOUSEHOLD_DIR}/calendar.ics")
    sandbox.fs.upload_file(household, f"{HOUSEHOLD_DIR}/household.md")


# ## Step 3 - Run the task
#
# Same invocation as `hermes.py`: one prompt, no approval prompts, output streamed.


def run_hermes(sandbox: Sandbox, timeout_secs: int) -> int:
    exit_code = 1
    for event in sandbox.exec_stream(
        "hermes",
        "chat",
        "--oneshot",
        "--yolo",
        "--query",
        PERSONAL_ASSISTANT_PROMPT,
        workdir=HOUSEHOLD_DIR,
        timeout_secs=timeout_secs,
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


# ## Step 4 - Bring the results home
#
# Everything Hermes wrote to `out/` is downloaded, whatever it is named, so a partial
# run still returns what it finished.


def download_outputs(sandbox: Sandbox, local_dir: Path) -> list[Path]:
    listing = sandbox.exec(
        "find", OUT_DIR, "-maxdepth", "1", "-type", "f", "-printf", "%f\n"
    )
    names = [name for name in listing.stdout_text.splitlines() if name]
    local_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []
    for name in names:
        destination = local_dir / name
        sandbox.fs.download_file(f"{OUT_DIR}/{name}", destination)
        downloaded.append(destination)
    return downloaded


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
        lifetime_secs=args.timeout + SETUP_ALLOWANCE_SECS,
    )
    client = SandboxClient.from_env()
    try:
        print("Starting sandbox (the first run builds the Hermes image)...")
        client.run(sandbox)
        upload_inputs(sandbox, args.calendar, args.household)

        config = hermes_config(args.base_url, args.model, key_env)
        sandbox.exec(
            "bash",
            "-c",
            f"printf %s {shlex.quote(config)} > ~/.hermes/config.yaml",
        )

        print(f"Running Hermes with {args.model}...\n")
        exit_code = run_hermes(sandbox, args.timeout)

        downloaded = download_outputs(sandbox, args.out)
        print(f"\n--- Files from Hermes, in {args.out} ---")
        for path in downloaded:
            print(f"  {path.name}  ({path.stat().st_size:,} bytes)")
        if not downloaded:
            print("  (none)")
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
        description="Use Hermes Agent in a Chalk sandbox to plan a household's week "
        "and research childcare."
    )
    parser.add_argument(
        "--calendar",
        type=Path,
        default=HERE / "sample_week.ics",
        help="Calendar export (.ics) for the week to plan. Default: a sample week.",
    )
    parser.add_argument(
        "--household",
        type=Path,
        default=HERE / "household.md",
        help="Notes about the household: people, location, constraints, budget. "
        "Default: notes for the sample family.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(f"hermes-assistant-{uuid.uuid4().hex[:8]}"),
        help="Local directory for Hermes' files. Default: a new directory here.",
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
        default=1200,
        help="Seconds Hermes may run before it is stopped. Default: 1200",
    )
    sys.exit(main(parser.parse_args()))
