"""Validate a frozen GPU plan and compile its complete entrypoint without claims."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib
import json
from pathlib import Path
import shlex
import sys


def preview(out, source_head, env_file):
    sys.path.insert(0, str(out))
    sys.path.insert(1, str(out / "guard"))
    gpu = importlib.import_module("gpu_smoke")
    guard = importlib.import_module("cpu_seed")
    plan = gpu.validate(out)
    phase = "0" * 32
    config = {
        "root": "/tmp/sglang-gpu-" + phase,
        "overlay": base64.b64encode((out / "overlay.tar.gz").read_bytes()).decode(),
        "gpu_payload": base64.b64encode((out / "gpu_payload.py").read_bytes()).decode(),
        "cpu_payload": base64.b64encode(
            (out / "preflight_payload.py").read_bytes()
        ).decode(),
        "source_hashes": plan["source_hashes"],
        "fixture_bytes": plan["fixture_bytes"],
        "fixture_sha256": plan["fixture_sha256"],
        "deadline": 0,
        "cache_diagnostic": plan["scenario"] == "native_cache_diagnostic",
    }
    program, _, progress_root = guard.make_guest_program(
        phase, gpu.guest_code(), config
    )
    compile(program, "frozen-gpu-entrypoint", "exec")
    compile(
        gpu.progress_poll(out, progress_root, config["root"]), "frozen-gpu-poll", "exec"
    )
    if len(program.encode()) > 120 * 1024:
        raise ValueError("Full frozen entrypoint exceeds120KiB argument bound")
    command = shlex.join(
        [
            sys.executable,
            str(out / "gpu_smoke.py"),
            "--execute",
            "--out",
            str(out),
            "--env-file",
            str(env_file),
        ]
    )
    return {
        "source_head": source_head,
        "plan_sha256": hashlib.sha256((out / "plan.json").read_bytes()).hexdigest(),
        "local_files": plan["local_files"],
        "full_entrypoint_bytes": len(program.encode()),
        "full_entrypoint_compiles": True,
        "poll_compiles": True,
        "command": command,
        "resources_claimed": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-head", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            preview(args.out.resolve(), args.source_head, args.env_file.resolve()),
            indent=2,
        )
    )
