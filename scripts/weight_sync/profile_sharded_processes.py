"""Exact owned process-group cleanup, including descendants after leader exit."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import sys
from pathlib import Path


def identity(pid, timeout=0.25):
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "pgid=", "-o", "stat=", "-o", "lstart="],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode == 1 and not result.stdout.strip():
        return None
    result.check_returncode()
    group, state, started = result.stdout.strip().split(maxsplit=2)
    if state.startswith("Z"):
        return None
    return {"pid": pid, "group": int(group), "started": started}


def members(known):
    if known["pid"] != known["group"] or known["pid"] <= 0:
        raise RuntimeError("Require an owned session/group leader")
    leader = identity(known["pid"])
    if leader is not None and leader != known:
        raise RuntimeError("Group leader identity reused; refuse unrelated group")
    result = subprocess.run(
        ["ps", "-axo", "pid=,pgid=,stat=,lstart="],
        check=True,
        capture_output=True,
        text=True,
        timeout=0.5,
    )
    active = []
    for line in result.stdout.splitlines():
        pid, group, state, started = line.strip().split(maxsplit=3)
        if int(group) == known["group"] and not state.startswith("Z"):
            active.append({"pid": int(pid), "group": int(group), "started": started})
    return active


def stop_group(known):
    before = members(known)
    if before:
        # The registered group ID remains owned while its descendants exist;
        # a reused live leader is rejected by members() before any signal.
        os.killpg(known["group"], signal.SIGKILL)
    until = time.monotonic() + 2
    while True:
        active = members(known)
        if not active:
            return {
                "identity": known,
                "nonzombie_members_absent": True,
                "stopped_members": before,
            }
        if time.monotonic() >= until:
            raise RuntimeError("Owned process-group descendants remain")
        time.sleep(0.05)


def run_fuse(root):
    """Independent OS monitor; shares only the exact worker-owned group."""
    from profile_plan import write_json

    request = json.loads((root / "request.json").read_bytes())
    ready = json.loads((root / "ready.json").read_bytes())
    known = ready["identity"]
    if (
        ready["phase_id"] != request["phase_id"]
        or os.getpgrp() != known["pid"]
        or known["group"] != known["pid"]
    ):
        raise RuntimeError("Monitor group/phase differs; refuse unrelated signal")
    try:
        write_json(
            root / "fuse-ready.json",
            {
                "phase_id": request["phase_id"],
                "identity": identity(os.getpid()),
                "worker_identity": known,
            },
        )
        while time.time() < request["deadline"]:
            if (
                identity(known["pid"]) != known
                or identity(request["parent_pid"]) != request["parent_identity"]
            ):
                break
            time.sleep(0.05)
    finally:
        os.killpg(os.getpgrp(), signal.SIGKILL)


def start_fuse(root, request):
    """Require monitor readiness before authorization can issue any API."""
    if os.getpgrp() != os.getpid():
        raise RuntimeError("Worker is not its own isolated group/session leader")
    child = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("profile_sharded_guard.py")),
            "--fuse",
            str(root),
        ],
        stdin=subprocess.DEVNULL,
    )
    while time.time() < request["deadline"]:
        path = root / "fuse-ready.json"
        if path.exists():
            value = json.loads(path.read_bytes())
            known = identity(child.pid)
            if (
                value
                != {
                    "phase_id": request["phase_id"],
                    "identity": known,
                    "worker_identity": identity(os.getpid()),
                }
                or known is None
                or known["group"] != os.getpid()
            ):
                raise RuntimeError("Monitor identity differs")
            return child
        if child.poll() is not None:
            raise RuntimeError("Independent cleanup monitor exited")
        time.sleep(0.01)
    raise TimeoutError("Independent monitor readiness deadline")


def registered_groups(ledger):
    record = json.loads(ledger.read_bytes())
    result = []
    for root in sorted(ledger.parent.glob("cleanup-attempt-*")):
        request = json.loads((root / "request.json").read_bytes())
        if request.get("phase_id") != record["cpu_phase_id"] or request.get(
            "ledger"
        ) != str(ledger):
            raise RuntimeError("Cleanup registry phase/ledger differs")
        ready_path = root / "ready.json"
        if not ready_path.exists():
            raise RuntimeError("Inert worker registration remains unresolved")
        ready = json.loads(ready_path.read_bytes())
        if ready.get("phase_id") != record["cpu_phase_id"]:
            raise RuntimeError("Cleanup registry identity phase differs")
        authorization = root / "authorization.json"
        if authorization.exists():
            if json.loads(authorization.read_bytes()) != ready:
                raise RuntimeError("Cleanup registry authorization differs")
            fuse = json.loads((root / "fuse-ready.json").read_bytes())
            if (
                fuse.get("phase_id") != record["cpu_phase_id"]
                or fuse.get("worker_identity") != ready["identity"]
                or fuse.get("identity", {}).get("group") != ready["identity"]["group"]
            ):
                raise RuntimeError("Cleanup monitor registry differs")
        result.append(ready["identity"])
    return result


def stop_registered(ledger):
    return [stop_group(known) for known in registered_groups(ledger)]


def prove_registered_absent(ledger):
    for known in registered_groups(ledger):
        if members(known):
            raise RuntimeError("Owned cleanup group members remain")
