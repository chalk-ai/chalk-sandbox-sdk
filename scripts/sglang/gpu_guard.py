"""Compose reviewed HOST cleanup with exact owned upload-writer termination."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import host_guard


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


def stop_upload(record, out):
    known = record.get("upload_writer")
    if known is None:
        return
    actual = process_identity(known["pid"])
    if actual is None:
        return
    if actual != known or known["group"] != known["pid"]:
        raise RuntimeError("Upload writer identity changed; refuse unrelated signal")
    os.killpg(known["pid"], signal.SIGTERM)
    until = time.time() + 5
    while process_identity(known["pid"]) is not None and time.time() < until:
        time.sleep(0.1)
    if process_identity(known["pid"]) is not None:
        if process_identity(known["pid"]) != known:
            raise RuntimeError("Upload writer identity changed during stop")
        os.killpg(known["pid"], signal.SIGKILL)
    # Parent may retain a zombie until wait(); a zombie cannot perform uploads.
    state = subprocess.run(
        ["ps", "-p", str(known["pid"]), "-o", "stat="],
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()
    if state and not state.startswith("Z"):
        raise RuntimeError("Upload writer not stopped before volume teardown")
    (out / "upload-writer-stopped.json").write_text(
        json.dumps({"identity": known, "stopped_at": time.time()})
    )


def release_fixture_data(guard, record, out, release):
    marker_path = out / "upload-create.json"
    if not marker_path.exists():
        # No create was permitted. In particular never delete a pre-existing
        # collision merely because the planned random name was registered.
        record["owned_volume_names"] = []
        return release(record)
    marker = json.loads(marker_path.read_bytes())
    if (
        marker["phase_id"] != record["cpu_phase_id"]
        or marker["name"] not in record["owned_volume_names"]
    ):
        raise RuntimeError("Fixture create ownership marker differs")
    if marker["status"] not in {"inflight", "acknowledged"}:
        raise RuntimeError("Unknown fixture create state")
    until = marker["reconcile_until_epoch"] if marker["status"] == "inflight" else 0
    if (
        marker["rpc_deadline_epoch"] - marker["started_at"] != 30
        or marker["reconcile_until_epoch"] != marker["started_at"] + 90
    ):
        raise RuntimeError("Fixture create deadline/reconciliation changed")
    while True:
        release(record)
        if time.time() >= until:
            break
        record["volume_verified_absent"] = False
        guard.write_json(out / "launcher.json", record)
        time.sleep(max(0, min(2, until - time.time())))
    record["fixture_create_reconciled_until_epoch"] = time.time()
    guard.write_json(out / "launcher.json", record)


def install(guard, out):
    host_guard.install_release_check(guard)
    original = guard.release_data

    def release(record):
        stop_upload(record, out)
        return release_fixture_data(guard, record, out, original)

    guard.release_data = release


def arm(guard, record, ledger):
    guard.write_json(ledger, record)
    with (ledger.parent / "cpu-watchdog.log").open("ab", buffering=0) as log:
        watch = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--watchdog", str(ledger)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    awake = subprocess.Popen(
        ["caffeinate", "-i", "-w", str(watch.pid)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    record.update(cpu_watchdog_pid=watch.pid, cpu_caffeinate_pid=awake.pid)
    guard.write_json(ledger, record)
    until = time.time() + 60
    while time.time() < until:
        if watch.poll() is not None:
            raise RuntimeError("GPU watchdog exited before readiness")
        marker = ledger.parent / "cpu-watchdog-ready.json"
        if marker.exists():
            ready = json.loads(marker.read_bytes())
            if (
                ready.get("phase_id") != record["cpu_phase_id"]
                or ready.get("pid") != watch.pid
            ):
                raise RuntimeError("GPU watchdog phase/PID differs")
            return watch
        time.sleep(0.5)
    raise TimeoutError("GPU watchdog not ready; no resource claims")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watchdog", type=Path, required=True)
    ledger = parser.parse_args().watchdog
    sys.path.insert(0, str(ledger.parent / "guard"))
    import cpu_seed

    install(cpu_seed, ledger.parent)
    cpu_seed.write_json(
        ledger.parent / "host-watchdog-inventory-ready.json",
        host_guard.runtime_absence(None),
    )
    cpu_seed.watchdog(ledger)
