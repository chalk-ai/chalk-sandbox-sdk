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

from agents import Agent, Runner, function_tool
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from chalkcompute import Container, Image, VolumeClient, VolumeNotFoundError
from chalkcompute._connect import ConnectClient

import ui
from bash_tool import WORKSPACE, format_result

load_dotenv()

# Demo output should be the agent loop, not infra plumbing.
for noisy in ("httpx", "chalkcompute", "openai", "openai._base_client"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

def _open_volume_client() -> VolumeClient:
    return VolumeClient.from_connect(ConnectClient())

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
    """Create the volume and upload the case packet — or reuse it if it exists."""
    with _open_volume_client() as vc:
        if _volume_exists(vc, vol_name):
            ui.volume_reused(vol_name)
            return

        ui.volume_creating(vol_name)
        vol = vc.create(vol_name)
        for f in sorted(case_dir.iterdir()):
            if f.is_file():
                data = f.read_bytes()
                vol.put_file(f.name, data)
                ui.volume_added(f.name, len(data))

def _volume_exists(vc: VolumeClient, name: str) -> bool:
    try:
        vc.lookup(name)
        return True
    except VolumeNotFoundError:
        return False


async def investigate(case: str) -> None:
    case_dir = SAMPLES / case
    if not case_dir.is_dir():
        raise SystemExit(f"unknown case: {case}")

    vol_name = f"kyc-case-{case.replace('_', '-')}"
    ensure_volume(case_dir, vol_name)

    image = (
        Image.debian_slim("3.12")
        .run_commands(
            "apt-get update",
            "apt-get install -y --no-install-recommends "
            "tesseract-ocr libimage-exiftool-perl poppler-utils jq",
            "rm -rf /var/lib/apt/lists/*",
        )
        .pip_install(["pypdf>=4.0", "pillow>=10.0", "exifread>=3.0"])
    )

    container = Container(
        image=image,
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

    @function_tool
    def bash(command: str) -> str:
        """Run a bash command in the sandbox. cwd=/workspace."""
        return format_result(container.exec("bash", "-lc", f"cd {WORKSPACE} && {command}"))

    agent = Agent(
        name="kyc-fraud-investigator",
        instructions=SYSTEM_PROMPT,
        model=MODEL,
        tools=[bash],
        output_type=Verdict,
    )

    try:
        ui.agent_starting()
        result = Runner.run_streamed(
            starting_agent=agent,
            input="Investigate the case packet at /workspace and return a Verdict.",
            max_turns=20,
        )
        await ui.stream(result)
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
