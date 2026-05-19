#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute", "python-dotenv"]
# ///


from __future__ import annotations

import argparse
import json
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from chalkcompute import (
    Container,
    Image,
    VolumeClient,
    VolumeError,
    VolumeFileNotFoundError,
    VolumeNotFoundError,
    build_image,
)
from chalkcompute._connect import ConnectClient
from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[2] / ".env")

logging.basicConfig(level=logging.INFO, format="[%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("hub")

_WORKER_SRC = Path(__file__).parent / "worker.py"

# ## Image
WORKER_IMAGE = (
    Image.base("python:3.12-slim")
    .run_commands(
        "apt-get update -qq && apt-get install -y -qq git curl ca-certificates 2>&1",
        "curl -fsSL https://api.chalk.ai/install.sh | sh 2>&1",
    )
    .pip_install(["anthropic>=0.40.0", "chalkpy>=2.0.0"])
    .workdir("/data")
    .env({"PATH": "/root/.chalk/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"})
)

# Default focus areas — generic prompts that nudge each worker toward a
DEFAULT_FOCUS_AREAS = [
    "Focus on transaction-level features (amount, merchant, timing). Look for unusual transactions.",
    "Focus on user-level features (account age, history, demographics). Look for risky user profiles.",
    "Focus on cross-feature interactions (transaction × user) and ratios. Look for behavior anomalies.",
]


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


# ## Per-worker thread body — spawn a Container with worker.py as entrypoint,

_WORKER_POLL_INTERVAL = 5.0
_WORKER_OVERALL_TIMEOUT = 1800  # 30 min hard cap


def _run_worker(
    *,
    worker_id: str,
    focus: str,
    volume_name: str,
    image_ref: str,
    base_env: dict[str, str],
    project_repo: str | None,
    project_ref: str,
    sample_inputs: str,
    pk_feature: str,
    max_turns: int,
) -> str:
    name = f"rule-searcher-w{worker_id}-{uuid.uuid4().hex[:4]}"
    env = {
        **base_env,
        "WORKER_ID": worker_id,
        "WORKER_FOCUS": focus,
        "PROMOTIONS_DIR": "/data/promotions",
        "WORKSPACE": "/workspace/project",
        "SAMPLE_INPUTS": sample_inputs,
        "PRIMARY_KEY_FEATURE": pk_feature,
        "MAX_TURNS": str(max_turns),
        "PROJECT_REF": project_ref,
        "SEARCH_BRANCH": f"rule-searcher-{worker_id}",
        "PROMOTE_BRANCH": "rule-promising",
    }
    if project_repo:
        env["PROJECT_REPO"] = project_repo

    log.info("[w%s] creating container %s...", worker_id, name)
    container = Container(
        image=image_ref,  # pre-built URI; skips the build step
        name=name,
        env=env,
        volumes=[(volume_name, "/data")],
        entrypoint=["python", "/data/worker.py"],
        lifetime=f"{_WORKER_OVERALL_TIMEOUT + 300}s",
    ).run()
    # Don't let stop() delete the shared promotions volume — every worker
    # mounts the same one and the hub reads from it after all workers exit.
    container._volumes = []

    log.info(
        "[w%s] container running (id=%s); follow logs with: "
        "chalk logs --query 'pod_name:chalk-container-%s' --start-time '30m ago' --follow",
        worker_id, container.id, name,
    )

    try:
        deadline = time.monotonic() + _WORKER_OVERALL_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(_WORKER_POLL_INTERVAL)
            info = container.refresh()
            if info.status in ("Succeeded", "Failed"):
                log.info("[w%s] container exited with status=%s", worker_id, info.status)
                return f"status={info.status}"
            if info.status == "Stopped":
                log.warning("[w%s] container Stopped unexpectedly", worker_id)
                return "status=Stopped"
        log.warning("[w%s] hit %ds overall timeout", worker_id, _WORKER_OVERALL_TIMEOUT)
        return "status=Timeout"
    finally:
        try:
            container.stop()
            log.info("[w%s] container stopped", worker_id)
        except Exception as e:  # noqa: BLE001
            log.warning("[w%s] stop failed: %s", worker_id, e)


# ## Aggregation — read JSONL files off the shared volume, dedup by feature.

def _aggregate(volume_name: str, num_workers: int) -> list[dict]:
    vol = VolumeClient.from_connect(ConnectClient()).from_name(volume_name)
    all_entries: list[dict] = []
    for i in range(num_workers):
        path = f"promotions/worker-{i}.jsonl"
        try:
            data = vol.read_file(path)
        # NOTE: the SDK maps NOT_FOUND to VolumeNotFoundError for both
        # "volume gone" and "file missing" — catch both so one absent
        # worker output doesn't blow up the whole aggregation.
        except (VolumeFileNotFoundError, VolumeNotFoundError):
            log.warning("worker %d wrote no promotions (no %s on volume)", i, path)
            continue
        for line in data.decode().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                all_entries.append(json.loads(line))
            except json.JSONDecodeError as e:
                log.warning("skipping malformed line in %s: %s", path, e)

    seen: set[str] = set()
    deduped: list[dict] = []
    for entry in all_entries:
        feat = entry.get("feature")
        if not feat or feat in seen:
            continue
        seen.add(feat)
        deduped.append(entry)
    return deduped


# ## Main

def main(
    project_repo: str | None,
    project_ref: str,
    num_workers: int,
    sample_inputs: str,
    pk_feature: str,
    max_turns: int,
    focus_areas: list[str],
    volume_name: str | None,
) -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is required (check your .env)")

    base_env = _passthrough_env()
    fleet_id = uuid.uuid4().hex[:6]
    volume_name = volume_name or f"rule-fleet-{fleet_id}"

    # 1. Create the shared volume (idempotent on reruns with --volume-name)
    #    and upload worker.py onto it. Workers will execute /data/worker.py.
    vol_client = VolumeClient.from_connect(ConnectClient())
    try:
        vol_client.create(volume_name)
        log.info("created volume %s", volume_name)
    except VolumeError as e:
        if "exist" not in str(e).lower():
            raise
        log.info("reusing existing volume %s", volume_name)
    vol = vol_client.from_name(volume_name)
    vol.put_file("worker.py", _WORKER_SRC.read_text())
    log.info("uploaded worker.py to %s://worker.py", volume_name)

    # 2. Pre-build the image via CustomImageService and reuse the registry
    #    URI for every worker container. Each Container(image=URI) call then
    #    skips the build step. First build is slow; reruns hit cache.
    log.info("building worker image via CustomImageService...")
    image_ref = build_image(WORKER_IMAGE)
    log.info("image built: %s", image_ref)

    # 3. Spawn worker containers in parallel — one thread per container.
    focuses = (focus_areas + [""] * num_workers)[:num_workers]
    log.info("dispatching %d worker(s) onto volume %r", num_workers, volume_name)
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=num_workers) as pool:
        futures = [
            pool.submit(
                _run_worker,
                worker_id=str(i),
                focus=focuses[i],
                volume_name=volume_name,
                image_ref=image_ref,
                base_env=base_env,
                project_repo=project_repo,
                project_ref=project_ref,
                sample_inputs=sample_inputs,
                pk_feature=pk_feature,
                max_turns=max_turns,
            )
            for i in range(num_workers)
        ]
        for i, fut in enumerate(futures):
            try:
                result = fut.result()
                log.info("worker %d returned: %s", i, result)
            except Exception:  # noqa: BLE001
                log.exception("worker %d crashed", i)

    log.info("all workers exited after %.1fs", time.monotonic() - t0)

    # 4. Aggregate promotions off the shared volume.
    promotions = _aggregate(volume_name, num_workers)
    if not promotions:
        log.warning("no promotions written by any worker")
        return

    print(f"\n=== Promoted rules ({len(promotions)} unique features) ===")
    for entry in promotions:
        print(
            f"  - {entry['feature']:40s}  metric={entry.get('metric', '?'):20s}  "
            f"worker={entry.get('worker_id', '?')}  rule={entry.get('rule_file', '?')}"
        )
    print(f"\nVolume: {volume_name}")
    print(f"  chalk volume list-files --name {volume_name}")
    print(f"  chalk volume get-file --name {volume_name} --path promotions/worker-0.jsonl")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Spawn a fleet of rule-searcher sandboxes")
    parser.add_argument(
        "--repo", type=str, default=os.environ.get("PROJECT_REPO"),
        help="GitHub repo (owner/name) for each worker to clone.",
    )
    parser.add_argument("--ref", type=str, default="main", help="Branch / tag / SHA")
    parser.add_argument("--workers", type=int, default=3, help="Number of parallel sandboxes")
    parser.add_argument(
        "--samples", type=str, default="1,2,3,4,5,6,7,8,9,10",
        help="Comma-separated primary-key values for backtest",
    )
    parser.add_argument("--pk-feature", type=str, default="user.id")
    parser.add_argument("--max-turns", type=int, default=24, help="Per-worker agent-turn cap")
    parser.add_argument(
        "--focus", action="append", default=None,
        help="Override default focus areas (repeat once per worker). "
             "If unset, uses transaction/user/cross-feature defaults.",
    )
    parser.add_argument(
        "--volume-name", type=str, default=None,
        help="Reuse an existing shared volume by name (omit to mint a fresh one)",
    )
    args = parser.parse_args()

    focus_areas = args.focus or DEFAULT_FOCUS_AREAS
    main(
        project_repo=args.repo,
        project_ref=args.ref,
        num_workers=args.workers,
        sample_inputs=args.samples,
        pk_feature=args.pk_feature,
        max_turns=args.max_turns,
        focus_areas=focus_areas,
        volume_name=args.volume_name,
    )
