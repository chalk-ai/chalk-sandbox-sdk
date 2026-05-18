"""Re-upload a case packet to its persistent Chalk volume. Use after editing
files in samples/ — story.py's volume step is no-op when the volume exists.

    uv run python -m demo_tools.refresh case_fraud

Also exposed as `--refresh` on story.py, which delegates here.
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from chalkcompute import VolumeClient, VolumeNotFoundError  # noqa: E402
from chalkcompute._connect import ConnectClient  # noqa: E402

SAMPLES = Path(__file__).parent.parent / "samples"


def refresh(case: str) -> None:
    case_dir = SAMPLES / case
    if not case_dir.is_dir():
        raise SystemExit(f"unknown case: {case}")
    vol_name = f"kyc-case-{case.replace('_', '-')}"
    local_files = {f.name for f in case_dir.iterdir() if f.is_file()}
    print(f"refreshing {vol_name} → {sorted(local_files)}")
    with VolumeClient.from_connect(ConnectClient()) as vc:
        try:
            vc.lookup(vol_name)
            vol = vc.from_name(vol_name)
        except VolumeNotFoundError:
            vol = vc.create(vol_name)
        # Remove files that exist on the volume but not in samples/.
        for info in vol.listdir():
            if info.path not in local_files:
                vol.remove_file(info.path)
                print(f"  removed {info.path}")
        # Upload (overwriting) everything in samples/.
        for f in sorted(case_dir.iterdir()):
            if f.is_file():
                vol.put_file(f.name, f.read_bytes())
                print(f"  uploaded {f.name} ({f.stat().st_size} bytes)")
    print("done")


def main() -> None:
    refresh(sys.argv[1] if len(sys.argv) > 1 else "case_fraud")


if __name__ == "__main__":
    main()
