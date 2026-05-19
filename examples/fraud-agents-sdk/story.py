"""KYC fraud investigator on Chalk Compute.

    uv run python story.py case-fraud
    uv run python story.py case-legit
"""

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Literal

from agents import Agent, Runner, function_tool
from dotenv import load_dotenv
from pydantic import BaseModel, Field

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from chalkcompute import Container, Image, Volume

import ui
from bash_tool import WORKSPACE, format_result

load_dotenv()

for noisy in ("httpx", "chalkcompute", "openai", "openai._base_client"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

SAMPLES = Path(__file__).parent / "samples"

SYSTEM_PROMPT = """\
You are a KYC fraud investigator. You have one tool: bash, executing in
a sandboxed container with the case packet at /workspace. Start by
listing /workspace — do not assume filenames or document types.

The container has tesseract, exiftool, pdftotext, jq, and python with
pillow + pypdf + exifread. Compose your own commands. 

Decide by identity correlation: the applicant's name and DOB must be
consistent across the application form, their identity document, and
any supporting documents .
Mismatch on name or DOB = high risk. All consistent = low risk.
EXIF alone is not enough to raise risk.

Supporting documents must be authenticated. Agreements must show a signature 
naming the applicant. Statements must show account-holder or owner metadata.
An unsigned lease or anonymous statement is not valid evidence — flag it 
even if the name matches.

Return a Verdict with specific signals (file + field + observation).
"""

class Verdict(BaseModel):
    risk: Literal["low", "medium", "high"]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    signals: list[str]


async def main(case: str) -> None:
    run_id = uuid.uuid4().hex[:6]
    vol_name = f"kyc-{case}-{run_id}"

    with Volume(vol_name) as volume:
        ui.volume_creating(vol_name)
        for f in (SAMPLES / case).iterdir():
            volume.put_file(f.name, f.read_bytes())
            ui.volume_added(f.name, f.stat().st_size)

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
        name=f"kyc-investigator-{run_id}",
        cpu="1",
        memory="2Gi",
        volumes=[(vol_name, WORKSPACE)],
        lifetime="600s",
    )
    ui.build_starting()
    container.run()
    ui.container_started(container)

    @function_tool
    def bash(thought: str, command: str) -> str:
        """Run a bash command in /workspace. `thought` is one sentence shown to the user explaining why you're running this command."""
        return format_result(container.exec("bash", "-lc", f"cd {WORKSPACE} && {command}"))

    agent = Agent(
        name="kyc-fraud-investigator",
        instructions=SYSTEM_PROMPT,
        model="gpt-4o",
        tools=[bash],
        output_type=Verdict,
    )

    ui.agent_starting()
    result = Runner.run_streamed(
        starting_agent=agent,
        input="Investigate the case packet at /workspace and return a Verdict.",
        max_turns=20,
    )
    await ui.stream(result)
    ui.verdict(result.final_output)
    ui.container_kept(container)


if __name__ == "__main__":
    case = sys.argv[1] if len(sys.argv) > 1 else "case-fraud"
    asyncio.run(main(case))
