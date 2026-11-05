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
    from profile_sharded_processes import members, prove_registered_absent

    load_env(args.env_file)
    record = json.loads(args.cpu_ledger.read_bytes())
    prove_registered_absent(args.cpu_ledger)
    if members(record["launcher_identity"]):
        raise RuntimeError("Owned controller group descendants remain")
    print(json.dumps(verify(args.cpu_ledger, None, args.out), indent=2))


if __name__ == "__main__":
    main()
