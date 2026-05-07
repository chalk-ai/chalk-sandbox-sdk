"""Tiny helper to compare two JSON files on a chosen set of keys. Saves the
agent from having to write its own diff logic in shell.

If either argument isn't a UTF-8 JSON file, emits a structured error JSON
instead of a Python traceback — the agent reads the message and either
chooses different inputs or pivots to OCR/parse_pdf output."""

import json
import sys
from pathlib import Path


def _load(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"{path}: file does not exist")
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ValueError(
            f"{path}: not a UTF-8 text file (looks binary — "
            f"did you pass an image or PDF? this tool only compares JSON files; "
            f"OCR/parse_pdf outputs are JSON, the underlying images and PDFs are not)"
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"{path}: not valid JSON ({e.msg} at line {e.lineno})")
    if not isinstance(data, dict):
        raise ValueError(f"{path}: JSON top-level must be an object, got {type(data).__name__}")
    return data


def main(path_a: str, path_b: str, keys: list[str]) -> int:
    try:
        a = _load(path_a)
        b = _load(path_b)
    except ValueError as e:
        print(json.dumps({"error": str(e)}, indent=2))
        return 1

    matches = {
        k: str(a.get(k, "")).strip().lower() == str(b.get(k, "")).strip().lower()
        for k in keys
    }
    print(json.dumps({"matches": matches, "a": a, "b": b}, indent=2))
    return 0 if all(matches.values()) else 1


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("usage: compare_fields.py <a.json> <b.json> <key1> [<key2> ...]", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3:]))
