"""Open everything in a case packet for visual inspection.

    uv run python show.py case_fraud
    uv run python show.py case_legit

Pops the ID / selfie / PDF in their default macOS apps, prints any JSON
documents in the packet, and prints the system prompt the agent will use.
Run this right before a demo recording so the viewer sees what's being
fed to the agent.
"""

import json
import subprocess
import sys
from pathlib import Path

from agent import SYSTEM_PROMPT

SAMPLES = Path(__file__).parent / "samples"
VISUAL_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}


def main() -> None:
    case = sys.argv[1] if len(sys.argv) > 1 else "case_fraud"
    case = case.removeprefix("samples/").rstrip("/")
    case_dir = SAMPLES / case

    if not case_dir.is_dir():
        available = ", ".join(p.name for p in SAMPLES.iterdir() if p.is_dir())
        print(f"unknown case: {case}", file=sys.stderr)
        print(f"available cases: {available}", file=sys.stderr)
        sys.exit(2)

    print()
    print("=" * 70)
    print(f"  case packet: {case}")
    print("=" * 70)
    print()

    visuals = sorted(f for f in case_dir.iterdir() if f.suffix.lower() in VISUAL_EXTENSIONS)
    if visuals:
        print("[visuals] opening in default app:")
        for v in visuals:
            print(f"          - {v.name}")
        subprocess.run(["open", *(str(v) for v in visuals)], check=False)
        print()

    for json_file in sorted(case_dir.glob("*.json")):
        print(f"--- {json_file.name} " + "-" * (66 - len(json_file.name)))
        try:
            data = json.loads(json_file.read_text())
            print(json.dumps(data, indent=2))
        except json.JSONDecodeError:
            print(json_file.read_text())
        print()

    print("=" * 70)
    print("  system prompt the agent will use")
    print("=" * 70)
    print(SYSTEM_PROMPT)


if __name__ == "__main__":
    main()
