"""CLI entrypoint.

    uv run python run.py case_fraud
    uv run python run.py case_legit
    uv run python run.py case_fraud --keep      # leave the container alive after the verdict

Walks through:
  1. Generate the sample packet if it's missing (PIL + reportlab, local only).
  2. Create a one-shot Volume, upload the case files into it.
  3. Build the investigator Image and start a Container with the volume mounted
     at /workspace.
  4. Hand the Container to an OpenAI Agents SDK Agent via three function tools
     (chalk_shell, chalk_read_file, chalk_list_workspace).
  5. Stream the agent run — print every tool call + truncated result as it
     happens, so the demo viewer sees the agent thinking out loud.
  6. Stop the container (unless --keep). Container.stop() also deletes the
     per-run volume.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path

from agents import Runner
from agents.items import MessageOutputItem, ToolCallItem, ToolCallOutputItem
from dotenv import load_dotenv

from chalkcompute import Container, VolumeClient, VolumeNotFoundError
from chalkcompute._connect import ConnectClient  # internal helper used to auth VolumeClient

from agent import build_agent
from image import investigator_image

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
# Quiet the noisy infra-level loggers; the demo cares about the agent loop,
# not every gRPC poll or OAuth refresh.
for noisy in ("httpx", "chalkcompute", "openai", "openai._base_client"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger(__name__)

WORKSPACE = "/workspace"
SAMPLES = Path(__file__).parent / "samples"

# Staging-specific. Update if you point this example at a different env.
DASHBOARD_BASE = (
    "https://staging.chalk.dev/projects/tmnmau5shs3qe"
    "/environments/tmnmc9beyujew/containers"
)


def _ensure_samples(case: str) -> Path:
    case_dir = SAMPLES / case
    if case_dir.is_dir() and any(case_dir.iterdir()):
        return case_dir
    log.info("samples missing — generating into %s", SAMPLES)
    from make_samples import make_samples

    make_samples(SAMPLES)
    if not case_dir.is_dir():
        raise SystemExit(f"unknown case: {case} (expected one of {os.listdir(SAMPLES)})")
    return case_dir


def _ensure_volume(case_dir: Path, vol_name: str, *, refresh: bool) -> None:
    """Ensure the case packet volume exists and is populated.

    First run for a case: create the volume + upload the four packet files.
    Subsequent runs: skip — the volume already has the files and ChalkFS
    can mount it warm. Pass ``refresh=True`` to force re-upload (useful
    after editing make_samples.py).
    """
    with VolumeClient.from_connect(ConnectClient()) as vol_client:
        if not refresh:
            try:
                vol_client.lookup(vol_name)
                print(f"[volume] {vol_name} already exists  ->  reusing (skip upload)")
                return
            except VolumeNotFoundError:
                pass

        # First-time setup, or --refresh forced.
        try:
            vol_client.lookup(vol_name)
            existed = True
        except VolumeNotFoundError:
            existed = False

        if existed:
            print(f"[volume] {vol_name} exists, --refresh: re-uploading packet")
            vol = vol_client.from_name(vol_name)
        else:
            print(f"[volume] {vol_name} missing  ->  creating + uploading packet")
            vol = vol_client.create(vol_name)

        for f in sorted(case_dir.iterdir()):
            if f.is_file():
                vol.put_file(f.name, f.read_bytes())
                print(f"         - {f.name} ({f.stat().st_size} bytes)")


def _print_container_banner(container: Container) -> None:
    cid = container._container_id or "(unknown)"
    print()
    print("┌" + "─" * 70 + "┐")
    print(f"│ container: {container._name:<58s}│")
    print(f"│ id:        {cid:<58s}│")
    print(f"│ dashboard: {DASHBOARD_BASE}/{cid}")
    print("└" + "─" * 70 + "┘")
    print()


def _truncate(s: str, n: int) -> str:
    s = str(s)
    if len(s) <= n:
        return s
    return s[:n] + f"  …[+{len(s) - n} chars]"


def _format_args(raw_args) -> str:
    """Tool args come as a JSON string. Pretty-print the most common shape."""
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
        except (ValueError, TypeError):
            return _truncate(raw_args, 200)
    else:
        parsed = raw_args
    if isinstance(parsed, dict) and len(parsed) == 1:
        # most of our tools take a single arg — render inline
        (k, v) = next(iter(parsed.items()))
        return f"{k}={v!r}" if not isinstance(v, str) else f"{k}={_truncate(v, 200)!r}"
    return _truncate(json.dumps(parsed), 200)


def _print_verdict(verdict) -> None:
    print()
    print("=" * 72)
    print(f"  VERDICT: {verdict.risk.upper()}   confidence={verdict.confidence:.2f}")
    print("=" * 72)
    print(f"\nReasoning:\n  {verdict.reasoning}\n")
    if verdict.signals:
        print("Signals:")
        for s in verdict.signals:
            print(f"  - {s}")
    print()


async def _run_streamed(agent, prompt: str, max_turns: int = 20):
    """Run the agent and live-print tool calls + outputs as they happen."""
    result = Runner.run_streamed(
        starting_agent=agent,
        input=prompt,
        max_turns=max_turns,
    )

    turn = 0
    async for event in result.stream_events():
        if event.type != "run_item_stream_event":
            # token-level deltas (raw_response_event) and agent handoff events
            # are skipped — they'd just be noise for this demo.
            continue
        item = event.item

        if isinstance(item, ToolCallItem):
            turn += 1
            raw = item.raw_item
            tool_name = getattr(raw, "name", None) or "?"
            args = getattr(raw, "arguments", None) or "{}"
            print(f"\n[{turn:02d}] → {tool_name}({_format_args(args)})")
        elif isinstance(item, ToolCallOutputItem):
            output = item.output if isinstance(item.output, str) else str(item.output)
            for line in _truncate(output, 600).splitlines():
                print(f"        {line}")
        elif isinstance(item, MessageOutputItem):
            # The model's natural-language thinking between tool calls. Most of
            # our runs end with the structured Verdict so this rarely fires.
            pass

    return result.final_output


async def investigate(case: str, *, keep: bool, refresh: bool) -> None:
    case_dir = _ensure_samples(case)

    run_id = uuid.uuid4().hex[:8]
    # Persistent per-case volume — survives across runs so the dashboard shows
    # the workspace and ChalkFS doesn't pay cold-sync each time. The container
    # itself is still ephemeral.
    vol_name = f"kyc-case-{case}"
    container_name = f"kyc-investigator-{run_id}"

    _ensure_volume(case_dir, vol_name, refresh=refresh)

    # The container needs Chalk credentials to back the FUSE mount of the
    # volume; everything else (OPENAI_API_KEY) lives on the host where the
    # agent loop runs, not inside the sandbox.
    sandbox_env = {
        k: v for k, v in os.environ.items()
        if k in ("CHALK_CLIENT_ID", "CHALK_CLIENT_SECRET")
    }

    container = Container(
        image=investigator_image(),
        name=container_name,
        cpu="1",
        memory="2Gi",
        volumes=[(vol_name, WORKSPACE)],
        env=sandbox_env,
        lifetime="600s",
    )

    print("[build] building image + starting investigator container...")
    container.run()
    _print_container_banner(container)

    try:
        agent = build_agent(container)
        print("[agent] investigation starting")

        verdict = await _run_streamed(
            agent,
            "Investigate the case packet at /workspace and return a Verdict.",
        )
        _print_verdict(verdict)
    finally:
        if keep:
            print("[keep]  leaving container running for inspection")
            print(f"        dashboard: {DASHBOARD_BASE}/{container._container_id}")
            print(f"        stop with:  uv run python stop.py {container._container_id}")
        else:
            print("[stop]  stopping container (case packet volume is persistent)")
            # Container.stop() iterates self._volumes and DeleteVolumes each one.
            # Drop the persistent case volume from that list so it survives;
            # leave the SDK's auto-created container-files-XXXX volume in place
            # so it gets reaped (those are unique per run and would orphan).
            container._volumes = [
                (n, p) for (n, p) in container._volumes if n != vol_name
            ]
            container.stop()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("case", nargs="?", default="case_fraud",
                        help="case_fraud or case_legit")
    parser.add_argument("--keep", action="store_true",
                        help="don't stop the container after the verdict")
    parser.add_argument("--refresh", action="store_true",
                        help="re-upload the case packet to the persistent volume "
                             "(use after editing make_samples.py)")
    args = parser.parse_args()
    case = args.case.removeprefix("samples/").rstrip("/")
    asyncio.run(investigate(case, keep=args.keep, refresh=args.refresh))


if __name__ == "__main__":
    main()
