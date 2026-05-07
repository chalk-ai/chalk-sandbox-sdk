"""OCR an image and emit structured fields the agent can grep. Wraps tesseract
and applies a few KYC-shaped regexes (NAME, DOB, ID); raw text is also printed
so the agent can fall back to free-form inspection."""

import json
import re
import subprocess
import sys


def main(path: str) -> int:
    res = subprocess.run(
        ["tesseract", path, "-", "--psm", "6"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        print(f"tesseract failed: {res.stderr}", file=sys.stderr)
        return res.returncode

    text = res.stdout
    fields: dict[str, str] = {}

    name = re.search(r"(?im)^\s*name[:\s]+(.+?)\s*$", text)
    if name:
        fields["name"] = name.group(1).strip()

    dob = re.search(r"(?im)\bDOB[:\s]+(\d{4}-\d{2}-\d{2})", text)
    if dob:
        fields["dob"] = dob.group(1)

    ident = re.search(r"(?im)\bID[:\s#]+([A-Z0-9-]+)", text)
    if ident:
        fields["id"] = ident.group(1)

    print(json.dumps({"fields": fields, "raw_text": text}, indent=2))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: ocr.py <path>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
