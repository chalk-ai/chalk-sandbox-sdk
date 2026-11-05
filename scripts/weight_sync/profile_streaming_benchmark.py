"""Measure isolated local synthetic fixture paths; all owned data is removed."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MAX_SECONDS = 120


def run(out, sdk_root, *, tensors=32, elements=1048576, timeout=120):
    if not 1 <= timeout <= MAX_SECONDS:
        raise ValueError("Subprocess deadline must be1..120seconds")
    script = Path(__file__).with_name("profile_streaming.py")
    out.mkdir(parents=True, exist_ok=False)
    env = {
        **os.environ,
        "PYTHONPATH": str(sdk_root),
        "OMP_NUM_THREADS": "2",
        "MKL_NUM_THREADS": "2",
    }
    began = time.time()
    owned_root = None
    try:
        with tempfile.TemporaryDirectory(prefix="chalk-synthetic-streaming-") as temp:
            root = Path(temp)
            owned_root = root
            source = root / "source"
            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--source",
                    str(source),
                    "--generate",
                    "--tensors",
                    str(tensors),
                    "--elements",
                    str(elements),
                ],
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            results = {}
            for mode in ("retained", "streaming"):
                target = root / mode
                process = subprocess.run(
                    [
                        sys.executable,
                        str(script),
                        "--source",
                        str(source),
                        "--out",
                        str(target),
                        "--mode",
                        mode,
                    ],
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                if len(process.stdout.encode()) > 128 * 1024:
                    raise ValueError("Synthetic result exceeds bounded report")
                results[mode] = json.loads(process.stdout)
                shutil.rmtree(target)  # Exact owned output, before next mode.
            for field in (
                "elements",
                "actual_changed_elements",
                "per_tensor_changed_elements",
                "canonical_tensor_hash_catalog_sha256",
            ):
                if results["retained"][field] != results["streaming"][field]:
                    raise RuntimeError(
                        "Retained/streaming proof results differ: " + field
                    )
            result = {
                "kind": "synthetic_streaming_fixture_assessment",
                "subprocess_deadline_seconds": timeout,
                "fixture_bytes": tensors * elements * 2,
                "fixture_is_real_model": False,
                "runs": results,
                "rss_reduction_bytes": results["retained"]["peak_rss_bytes"]
                - results["streaming"]["peak_rss_bytes"],
                "logical_disk_reduction_bytes": results["retained"][
                    "peak_logical_disk_bytes"
                ]
                - results["streaming"]["peak_logical_disk_bytes"],
                "full_8b_projection_measured": False,
                "current_frozen_profile_gate_changed": False,
                "remote_resources_claimed": False,
                "started_at": began,
                "finished_at": time.time(),
            }
    finally:
        released = owned_root is None or not owned_root.exists()
        (out / "local-cleanup.json").write_text(
            json.dumps(
                {"owned_tempdir_removed": released, "checked_at": time.time()}, indent=2
            )
        )
        if not released:
            raise RuntimeError("Owned synthetic tempdir cleanup unconfirmed")
    result["owned_tempdir_removed"] = True
    (out / "result.json").write_text(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sdk-root", type=Path, required=True)
    parser.add_argument("--tensors", type=int, default=32)
    parser.add_argument("--elements", type=int, default=1048576)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    value = run(
        args.out,
        args.sdk_root,
        tensors=args.tensors,
        elements=args.elements,
        timeout=args.timeout,
    )
    print(json.dumps({k: v for k, v in value.items() if k != "runs"}, indent=2))
