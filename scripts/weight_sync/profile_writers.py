"""Bounded PIN writes: child starts inert and exact identity is recorded before auth."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from profile_plan import write_json


def process_identity(pid):
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "pgid=", "-o", "lstart="],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode == 1 and not result.stdout.strip():
        return None
    result.check_returncode()
    group, started = result.stdout.strip().split(maxsplit=1)
    return {"pid": pid, "group": int(group), "started": started}


def stop_writer(record, out):
    known = record.get("pin_writer")
    if known is None:
        return
    actual = process_identity(known["pid"])
    if actual is None:
        return
    if actual != known or known["group"] != known["pid"]:
        raise RuntimeError("PIN writer identity changed; refuse unrelated signal")
    os.killpg(known["pid"], signal.SIGTERM)
    until = time.time() + 5
    while process_identity(known["pid"]) is not None and time.time() < until:
        time.sleep(0.1)
    if process_identity(known["pid"]) is not None:
        if process_identity(known["pid"]) != known:
            raise RuntimeError("PIN writer identity changed during stop")
        os.killpg(known["pid"], signal.SIGKILL)
    until = time.time() + 5
    while True:
        state = subprocess.run(
            ["ps", "-p", str(known["pid"]), "-o", "stat="],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        if not state or state.startswith("Z"):
            break
        if time.time() >= until:
            raise RuntimeError("PIN writer still active before volume teardown")
        if process_identity(known["pid"]) != known:
            raise RuntimeError("PIN writer identity changed during kill")
        time.sleep(0.1)
    write_json(
        out / "pin-writer-stopped.json", {"identity": known, "stopped_at": time.time()}
    )


def write_pin(guard, version):
    if type(version) is not int or version not in (0, 1):
        raise ValueError("Only reviewed target/restore PIN writes allowed")
    guard.check_budget()
    stop_writer(guard.record, guard.out)
    root = guard.out / ("pin-target" if version else "pin-restore")
    root.mkdir(exist_ok=False)
    request = {
        "phase_id": guard.record["gpu_phase_id"],
        "name": guard.name,
        "version": version,
        "deadline": min(time.time() + 30, guard.record["gpu_deadline_epoch"] - 180),
        "parent_pid": os.getpid(),
    }
    write_json(root / "request.json", request)
    with (root / "writer.log").open("ab", buffering=0) as log:
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--child", str(root)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    try:
        while time.time() < request["deadline"]:
            guard.check_budget()
            if child.poll() is not None:
                raise RuntimeError("Inert PIN writer exited before authorization")
            ready = root / "ready.json"
            if ready.exists():
                value = json.loads(ready.read_bytes())
                known = process_identity(child.pid)
                if (
                    value != {"phase_id": request["phase_id"], "identity": known}
                    or known is None
                    or known["group"] != child.pid
                ):
                    raise RuntimeError("Inert PIN writer identity/phase differs")
                guard.record["pin_writer"] = known
                guard.save()  # Durable before the child can issue any write.
                if (
                    json.loads((guard.out / "launcher.json").read_bytes()).get(
                        "pin_writer"
                    )
                    != known
                ):
                    raise RuntimeError("PIN writer registration was not persisted")
                write_json(root / "authorization.json", value)
                child.wait(timeout=max(0.01, request["deadline"] - time.time()))
                result = json.loads((root / "result.json").read_bytes())
                if child.returncode or result != {
                    "phase_id": request["phase_id"],
                    "version": version,
                    "written": True,
                }:
                    raise RuntimeError(
                        "PIN write did not acknowledge exact phase/version"
                    )
                return result
            time.sleep(0.05)
        raise TimeoutError("PIN writer readiness deadline expired")
    finally:
        # Before durable registration the child is inert; kill using our Popen.
        if guard.record.get("pin_writer", {}).get("pid") == child.pid:
            stop_writer(guard.record, guard.out)
        elif child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def child(root):
    request = json.loads((root / "request.json").read_bytes())
    ready = {"phase_id": request["phase_id"], "identity": process_identity(os.getpid())}
    write_json(root / "ready.json", ready)
    # No SDK imports, credentials use or writes until explicit parent authorization.
    while time.time() < request["deadline"]:
        os.kill(request["parent_pid"], 0)
        auth = root / "authorization.json"
        if auth.exists():
            if json.loads(auth.read_bytes()) != ready:
                raise RuntimeError("PIN child authorization identity/phase differs")
            break
        time.sleep(0.05)
    else:
        raise TimeoutError("Inert PIN child expired")
    if time.time() >= request["deadline"]:
        raise TimeoutError("PIN write lease expired")
    import chalkcompute as cc
    from chalkcompute._weights_format import pointer_json

    # Existing-only prevents metadata creation after the CPU owner is gone.
    cc.Volume(request["name"], create_if_missing=False).put_file(
        "PIN", pointer_json(request["version"])
    )
    write_json(
        root / "result.json",
        {
            "phase_id": request["phase_id"],
            "version": request["version"],
            "written": True,
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", type=Path, required=True)
    child(parser.parse_args().child)
