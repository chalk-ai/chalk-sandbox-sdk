"""Read-only independent release proof for the separate small synthetic plan."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from profile_sharded_plan import activate_controller_sdk, validate_ledger


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpu-ledger", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    validate_ledger(args.cpu_ledger)
    activate_controller_sdk(args.cpu_ledger.parent)
    from ftqa_smoke import load_env
    from profile_verify import verify
    from profile_writers import process_identity

    load_env(args.env_file)
    record = json.loads(args.cpu_ledger.read_bytes())
    if any(
        process_identity(v["pid"]) is not None
        for v in record.get("cleanup_workers", [])
    ):
        raise RuntimeError("Owned cleanup worker process remains")
    print(json.dumps(verify(args.cpu_ledger, None, args.out), indent=2))


if __name__ == "__main__":
    main()
