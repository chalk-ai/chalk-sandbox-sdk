#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute", "python-dotenv"]
# ///

# # Rule-searcher container — proof of concept
#
# Deploys a Chalk container that runs an LLM-driven agent against your
# checked-out Chalk feature project. The agent reads existing features and
# resolvers, proposes candidate rules for a downstream rules engine, applies
# them on a `rule-searcher` branch via `chalk apply --branch rule-searcher`,
# backtests via `chalk query --branch rule-searcher`, and promotes promising
# rules to a `rule-promising` branch — emitting a JSONL log entry per
# promotion onto a mounted Chalk volume.
#
# This is intentionally minimal. Run it:
#
#     just env
#     uv run python examples/rule-searcher/deploy.py \
#         --repo your-org/your-chalk-project \
#         --ref main \
#         --pk-feature user.id \
#         --samples 1,2,3,4,5,6,7,8,9,10


from __future__ import annotations

import argparse
import logging
import os
import time
import uuid
from pathlib import Path

from chalkcompute import Container, Image, VolumeClient, VolumeError
from chalkcompute._connect import ConnectClient
from dotenv import load_dotenv

# `.env` lives at the repo root.
load_dotenv(Path(__file__).parents[2] / ".env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

_AGENT_SRC = str(Path(__file__).parent / "agent.py")

## Image
IMAGE = (
    Image.base("python:3.12-slim")
    .run_commands(
        "apt-get update -qq && apt-get install -y -qq git curl ca-certificates 2>&1",
        "curl -fsSL https://api.chalk.ai/install.sh | sh 2>&1",
    )
    .pip_install([
        "anthropic>=0.40.0",
        "chalkpy>=2.0.0",
    ])
    .add_local_file(_AGENT_SRC, "/app/agent.py")
    .workdir("/app")
    .env({"PATH": "/root/.chalk/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"})
)


def _passthrough_env() -> dict[str, str]:
    keys = (
        "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN",
        "CHALK_CLIENT_ID",
        "CHALK_CLIENT_SECRET",
        "CHALK_API_SERVER",
        "CHALK_ENVIRONMENT_ID",
        "CHALK_ACTIVE_ENVIRONMENT",
    )
    env = {k: v for k in keys if (v := os.environ.get(k))}
    if "CHALK_ACTIVE_ENVIRONMENT" not in env and "CHALK_ENVIRONMENT_ID" in env:
        env["CHALK_ACTIVE_ENVIRONMENT"] = env["CHALK_ENVIRONMENT_ID"]
    return env


def main(
    name: str,
    project_repo: str | None,
    project_ref: str,
    sample_inputs: str,
    pk_feature: str,
    max_turns: int,
    timeout_secs: int,
    persist_promotions: bool,
) -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is required (check your .env)")

    env: dict[str, str] = {
        "PROJECT_REF": project_ref,
        "SAMPLE_INPUTS": sample_inputs,
        "PRIMARY_KEY_FEATURE": pk_feature,
        "MAX_TURNS": str(max_turns),
        "WORKSPACE": "/workspace/project",
        **_passthrough_env(),
    }
    if project_repo:
        env["PROJECT_REPO"] = project_repo

    volumes: list[tuple[str, str]] = []
    if persist_promotions:
        volume_name = f"{name}-promotions"
        volume_client = VolumeClient.from_connect(ConnectClient())
        try:
            volume_client.create(volume_name)
            log.info("volume created: %s", volume_name)
        except VolumeError as e:
            if "exist" not in str(e).lower():
                raise
            log.info("volume %s already exists; reusing", volume_name)
        volumes.append((volume_name, "/data"))
        env["PROMOTIONS_LOG"] = "/data/promotions.jsonl"
    else:
        env["PROMOTIONS_LOG"] = "/tmp/promotions.jsonl"

    container = Container(
        image=IMAGE,
        name=name,
        env=env,
        volumes=volumes,
        entrypoint=["python", "/app/agent.py"],
        lifetime="3600s",
    ).run()

    log.info("container started: id=%s name=%s", container.id, name)
    if container.info and container.info.web_url:
        log.info("web url: %s", container.info.web_url)

    try:
        log.info("agent running for up to %ds (Ctrl-C to stop early)", timeout_secs)
        time.sleep(timeout_secs)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        container.stop()
        if persist_promotions:
            log.info("container stopped. promotions persisted on volume %r", volumes[0][0])
        else:
            log.info("container stopped. grep `chalk logs` for 'PROMOTED' to see results.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deploy the rule-searcher agent container")
    parser.add_argument(
        "--name", type=str,
        default=f"rule-searcher-{uuid.uuid4().hex[:6]}",
        help="Container name. Default: rule-searcher-<random>",
    )
    parser.add_argument(
        "--repo", type=str, default=os.environ.get("PROJECT_REPO"),
        help="GitHub repo (owner/name) to clone. If omitted, the container runs `chalk source`.",
    )
    parser.add_argument("--ref", type=str, default="main", help="Branch / tag / SHA to check out.")
    parser.add_argument(
        "--samples", type=str, default="1,2,3,4,5,6,7,8,9,10",
        help="Comma-separated primary-key values to backtest against.",
    )
    parser.add_argument(
        "--pk-feature", type=str, default="user.id",
        help="Primary-key feature name used as input to chalk_query (e.g. 'user.id').",
    )
    parser.add_argument("--max-turns", type=int, default=24, help="Max agent turns.")
    parser.add_argument("--timeout", type=int, default=1800, help="Container lifetime in seconds.")
    parser.add_argument(
        "--persist-promotions", action="store_true",
        help="Mount a Chalk volume at /data and write promotions.jsonl there. "
             "Requires the service token to have `deploy.create` permission. "
             "Off by default — promotions still emit to stdout (chalk logs).",
    )
    args = parser.parse_args()

    main(
        name=args.name,
        project_repo=args.repo,
        project_ref=args.ref,
        sample_inputs=args.samples,
        pk_feature=args.pk_feature,
        max_turns=args.max_turns,
        timeout_secs=args.timeout,
        persist_promotions=args.persist_promotions,
    )
