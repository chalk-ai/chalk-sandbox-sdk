"""The agent definition: a single-purpose KYC fraud investigator that decides
which forensics commands to run by reasoning about the case, not by following
a hard-coded pipeline.

The structured ``Verdict`` output type makes the final answer easy to parse;
everything else (the *reasoning*, the *commands chosen*) is what we want the
viewer to see during a demo recording.
"""

from typing import Literal

from agents import Agent
from pydantic import BaseModel, Field

from chalkcompute import Container

from tools import build_tools

MODEL = "gpt-4o"

SYSTEM_PROMPT = """\
You are a KYC fraud investigator. A case packet has been mounted at /workspace
inside an isolated sandbox running in the customer's own cloud. Your job is to
decide whether the application is fraudulent and explain your reasoning.

You have shell access to the sandbox via the chalk_shell tool. The workspace
contains the files relevant to this case (typically: an ID image, a selfie,
a utility bill PDF, and an application JSON). The image already has these
forensics scripts at /opt/tools/ — invoke them via `python` (they are not
chmod +x):

  - python /opt/tools/ocr.py <image>            tesseract OCR + extract NAME/DOB/ID
  - python /opt/tools/parse_pdf.py <pdf>        extract text from a PDF
  - python /opt/tools/check_exif.py <image>     EXIF metadata + cheap consistency flags
  - python /opt/tools/compare_fields.py <a.json> <b.json> <key1> [<key2>...]
                                                compare two JSON files on chosen keys

You also have standard Unix tools: cat, ls, jq, grep, head, exiftool.

Tesseract OCR is imperfect — it sometimes mis-reads characters (e.g. "S" → "$").
If a regex-extracted field looks wrong, fall back to the raw_text in the OCR
output and use the name/DOB instead, which OCR usually gets right.

Investigate by running commands. The PRIMARY signal is identity correlation
across documents — the application JSON, the ID image (via OCR), and the
utility bill should all name the same person with consistent DOB. If those
agree, the case is low risk; if any of them disagree, the case is high risk.

Secondary signals (EXIF metadata, image artifacts) are weak on their own —
synthetic test data and ID-card scans routinely lack camera EXIF. Only flag
those as risk drivers if the primary identity correlation is also broken or
ambiguous. Do not return medium/high risk solely because of missing EXIF.

When you have enough signal, return a structured Verdict. Be specific in
your signals — name the file, the field, and what you observed.
"""


class Verdict(BaseModel):
    risk: Literal["low", "medium", "high"]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    signals: list[str]


def build_agent(container: Container) -> Agent:
    return Agent(
        name="kyc-fraud-investigator",
        instructions=SYSTEM_PROMPT,
        model=MODEL,
        tools=build_tools(container),
        output_type=Verdict,
    )
