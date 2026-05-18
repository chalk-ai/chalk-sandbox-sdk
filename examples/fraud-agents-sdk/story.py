"""KYC fraud investigator on Chalk Compute.

    uv run python story.py case_fraud
    uv run python story.py case_legit

Chalk Compute hosts a sandboxed container — your image, your VPC, your GPU.
The agent has one tool (bash) and composes its own forensics commands. PII
stays in the sandbox; only commands and text outputs round-trip to OpenAI.
"""

import argparse
import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Literal

from agents import Agent, Runner
from agents.items import ToolCallItem, ToolCallOutputItem
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from chalkcompute import Container, VolumeClient, VolumeNotFoundError
from chalkcompute._connect import ConnectClient

import ui
from bash_tool import WORKSPACE, make_bash_tool
from image import investigator_image

load_dotenv()

# Demo output should be the agent loop, not infra plumbing.
for noisy in ("httpx", "chalkcompute", "openai", "openai._base_client"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

SAMPLES = Path(__file__).parent / "samples"
MODEL = "gpt-4o"

SYSTEM_PROMPT = """\
You are a KYC fraud investigator. You have one tool: bash, executing in
a sandboxed container with the case packet at /workspace. Start by
listing /workspace — do not assume filenames.

The container has tesseract, exiftool, pdftotext, jq, and python with
pillow + pypdf + exifread. Compose whatever commands you need.

Decide by identity correlation: name and DOB should agree across the
application, the ID, and the utility bill. Mismatch = high risk; all
consistent = low risk. EXIF alone is not enough to raise risk.

Return a Verdict with specific signals (file + field + observation).
"""


class Verdict(BaseModel):
    risk: Literal["low", "medium", "high"]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    signals: list[str]


def ensure_volume(case_dir: Path, vol_name: str) -> None:
    with VolumeClient.from_connect(ConnectClient()) as vc:
        try:
            vc.lookup(vol_name)
            ui.volume_reused(vol_name)
            return
        except VolumeNotFoundError:
            pass
        ui.volume_creating(vol_name)
        vol = vc.create(vol_name)
        for f in sorted(case_dir.iterdir()):
            if f.is_file():
                vol.put_file(f.name, f.read_bytes())
                ui.volume_added(f.name, f.stat().st_size)


async def investigate(case: str) -> None:
    case_dir = SAMPLES / case
    if not case_dir.is_dir():
        raise SystemExit(f"unknown case: {case}")

    vol_name = f"kyc-case-{case.replace('_', '-')}"
    ensure_volume(case_dir, vol_name)

    container = Container(
        image=investigator_image(),
        name=f"kyc-investigator-{uuid.uuid4().hex[:8]}",
        cpu="1",
        memory="2Gi",
        volumes=[(vol_name, WORKSPACE)],
        env={
            "CHALK_CLIENT_ID": os.environ["CHALK_CLIENT_ID"],
            "CHALK_CLIENT_SECRET": os.environ["CHALK_CLIENT_SECRET"],
        },
        lifetime="600s",
    )

    ui.build_starting()
    container.run()
    ui.container_started(container)

    agent = Agent(
        name="kyc-fraud-investigator",
        instructions=SYSTEM_PROMPT,
        model=MODEL,
        tools=[make_bash_tool(container)],
        output_type=Verdict,
    )

    try:
        ui.agent_starting()
        result = Runner.run_streamed(
            starting_agent=agent,
            input="Investigate the case packet at /workspace and return a Verdict.",
            max_turns=20,
        )
        # The agent can emit multiple parallel ToolCallItems in one turn before
        # any ToolCallOutputItem fires — buffer them and pair on output so the
        # viewer sees call → output, call → output, not 3 calls then 3 outputs.
        pending: list = []
        turn = 0
        async for event in result.stream_events():
            if event.type != "run_item_stream_event":
                continue
            item = event.item
            if isinstance(item, ToolCallItem):
                pending.append(item)
            elif isinstance(item, ToolCallOutputItem):
                if pending:
                    turn += 1
                    ui.tool_call(turn, pending.pop(0))
                ui.tool_output(item)
        ui.verdict(result.final_output)
    finally:
        ui.container_stopping()
        # Keep the persistent case volume alive across runs; let the SDK
        # reap its own per-run container-files-XXXX volume.
        container._volumes = [(n, p) for (n, p) in container._volumes if n != vol_name]
        container.stop()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("case", nargs="?", default="case_fraud", help="case_fraud or case_legit")
    p.add_argument("--keep", action="store_true",
                   help="leave the container alive after the verdict (delegates to demo_tools.keep)")
    p.add_argument("--refresh", action="store_true",
                   help="re-upload the case packet before investigating (delegates to demo_tools.refresh)")
    args = p.parse_args()
    case = args.case.removeprefix("samples/").rstrip("/")

    if args.refresh:
        from demo_tools.refresh import refresh
        refresh(case)

    if args.keep:
        from demo_tools.keep import keep
        asyncio.run(keep(case))
    else:
        asyncio.run(investigate(case))


if __name__ == "__main__":
    main()
