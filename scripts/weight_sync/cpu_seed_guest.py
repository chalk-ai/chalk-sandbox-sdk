"""Single container-entrypoint seed, durable proof, and replay-safe read-only status.

This file has no automatic work on import. The launcher inlines it into one
NEVER-restarting sandbox entrypoint; ExecCommand only calls read_progress.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
import traceback
from pathlib import Path


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2))
    temporary.replace(path)


def run_seed_once(root: Path, phase, code, config):
    root.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            root / "started.lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
    except FileExistsError:
        return False  # A second entrypoint invocation cannot rerun publication.
    started = time.time()
    with os.fdopen(descriptor, "w") as handle:
        json.dump(
            {"phase_id": phase, "started_at": started, "pid": os.getpid()}, handle
        )
        handle.flush()
        os.fsync(handle.fileno())
    atomic_json(
        root / "started.json",
        {"phase_id": phase, "started_at": started, "pid": os.getpid()},
    )
    result = {"phase_id": phase, "started_at": started}
    with (
        (root / "stdout.txt").open("w", buffering=1) as stdout,
        (root / "stderr.txt").open("w", buffering=1) as stderr,
    ):
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                namespace = {"CONFIG": config}
                exec(compile(code, "cpu-seed-entrypoint", "exec"), namespace)
                proof = namespace["proof"]
                atomic_json(root / "proof.json", proof)
                result.update(
                    status="passed",
                    proof_sha256=hashlib.sha256(
                        (root / "proof.json").read_bytes()
                    ).hexdigest(),
                )
            except BaseException as error:
                traceback.print_exc()
                result.update(
                    status="failed", error_type=type(error).__name__, error=str(error)
                )
    result["finished_at"] = time.time()
    result["elapsed_seconds"] = result["finished_at"] - started
    atomic_json(root / "completed.json", result)
    return True


def tail(path, limit=65536):
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - limit))
        return handle.read(limit).decode(errors="replace")


def read_progress(root: Path):
    """No writes, process creation, publication, or compute allocation."""
    result = {
        "observed_at": time.time(),
        "stdout": tail(root / "stdout.txt"),
        "stderr": tail(root / "stderr.txt"),
    }
    for name in ("started", "completed"):
        path = root / (name + ".json")
        if path.exists():
            result[name] = json.loads(path.read_text())
    proof = root / "proof.json"
    if result.get("completed", {}).get("status") == "passed":
        payload = proof.read_bytes()
        if hashlib.sha256(payload).hexdigest() != result["completed"]["proof_sha256"]:
            raise RuntimeError("Durable CPU proof checksum mismatch")
        result["proof"] = json.loads(payload)
    for line in result["stdout"].splitlines():
        if line.startswith("CPU_SEED_ATOMIC="):
            try:
                result["atomic_seed"] = json.loads(
                    line.removeprefix("CPU_SEED_ATOMIC=")
                )
            except ValueError:
                pass  # The writer may still be finishing this progress line.
    return result
