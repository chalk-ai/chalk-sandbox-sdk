"""Copy the small trainer report from an existing owned smoke volume."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ftqa_smoke import load_env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument(
        "--full", action="store_true", help="Include per-tensor change maps"
    )
    args = parser.parse_args()
    record = json.loads(args.ledger.read_text())
    load_env(args.env_file)
    import chalkcompute as cc
    from chalkcompute._volume import VolumeFileNotFoundError

    try:
        data = cc.Volume(record["name"], create_if_missing=False).read_file(
            "evidence/trainer.json"
        )
    except VolumeFileNotFoundError:
        print(
            json.dumps(
                {
                    "status": "no_report_yet",
                    "training_run_id": record.get("training_run_id"),
                }
            )
        )
        return
    report = json.loads(data)
    (args.ledger.parent / "trainer-live.json").write_bytes(data)
    print(
        json.dumps(
            {
                "status": report.get("status"),
                "phases": report.get("phases", [])[-4:],
                "steps": [
                    step
                    if args.full
                    else {
                        key: value
                        for key, value in step.items()
                        if key != "changed_tensors"
                    }
                    for step in report.get("steps", [])
                ],
            }
        )
    )


if __name__ == "__main__":
    main()
