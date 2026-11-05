"""Independent small-fixture watchdog: work stops at lease minus120seconds."""

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
from profile_writers import process_identity


def install(ledger):
    from profile_sharded_plan import activate_controller_sdk, validate_ledger

    validate_ledger(ledger)
    activate_controller_sdk(ledger.parent)
    import cpu_seed as guard
    import profile_host_guard as host
    from profile_cpu_lifecycle import install as install_lifecycle
    from profile_volumes import install_cpu_cleanup

    install_lifecycle(guard, ledger)
    host.install_release_check(guard, ledger)
    install_cpu_cleanup(guard, ledger.parent)
    return guard


def cleanup_attempt(ledger):
    """One attempt; the supervisor owns retry/deadline and durable history."""
    guard = install(ledger)
    record = json.loads(ledger.read_bytes())
    from chalkcompute._connect import ConnectClient

    client = ConnectClient()
    client._ensure_auth()
    if client._env_id != record["environment_id"]:
        raise RuntimeError("Cleanup environment differs")
    guard.release_cpu(record, ledger.parent)
    if record.get("submission_inflight"):
        if time.time() < record.get("create_reconcile_until_epoch", 0):
            raise RuntimeError("Create reconciliation horizon still open")
        record["submission_inflight"] = False
        write_json(ledger, record)
    guard.release_data(record)
    record["status"] = "released"
    write_json(ledger, record)


def stop_attempt(child, known):
    from profile_sharded_processes import stop_group

    stop_group(known)  # Includes descendants even when the leader already exited.
    child.wait(timeout=2)


def attempt_process(root):
    """Starts inert: no SDK/auth/API until durable exact identity registration."""
    request = json.loads((root / "request.json").read_bytes())
    ready = {"phase_id": request["phase_id"], "identity": process_identity(os.getpid())}
    write_json(root / "ready.json", ready)
    from profile_sharded_processes import start_fuse

    start_fuse(
        root, request
    )  # Remains active after authorization and inside API calls.
    while time.time() < request["deadline"]:
        os.kill(request["parent_pid"], 0)
        path = root / "authorization.json"
        if path.exists():
            if json.loads(path.read_bytes()) != ready:
                raise RuntimeError("Cleanup worker registration differs")
            break
        time.sleep(0.05)
    else:
        raise TimeoutError("Inert cleanup worker expired")
    try:
        try:
            cleanup_attempt(Path(request["ledger"]))
        except Exception as error:
            write_json(
                root / "result.json",
                {
                    "phase_id": request["phase_id"],
                    "released": False,
                    "error_type": type(error).__name__,
                },
            )
        else:
            write_json(
                root / "result.json",
                {"phase_id": request["phase_id"], "released": True},
            )
    finally:
        # A successful or failed worker never leaves an inherited SSH/kubectl
        # descendant group behind. Parent treats the structured result as data.
        os.killpg(os.getpid(), signal.SIGKILL)


def run_attempt(ledger, record, number, *, cleanup_deadline=None):
    root = ledger.parent / ("cleanup-attempt-" + str(number))
    root.mkdir(exist_ok=False)
    deadline = min(
        (cleanup_deadline or record["cpu_deadline_epoch"]) - 2, time.time() + 45
    )
    request = {
        "phase_id": record["cpu_phase_id"],
        "ledger": str(ledger),
        "parent_pid": os.getpid(),
        "parent_identity": process_identity(os.getpid()),
        "deadline": deadline,
    }
    write_json(root / "request.json", request)
    with (root / "worker.log").open("ab", buffering=0) as log:
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--attempt", str(root)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    known = None
    try:
        while time.time() < deadline:
            ready_path = root / "ready.json"
            if ready_path.exists():
                if not (root / "fuse-ready.json").exists():
                    if child.poll() is not None:
                        raise RuntimeError("Worker exited before monitor readiness")
                    time.sleep(0.01)
                    continue
                ready = json.loads(ready_path.read_bytes())
                known = process_identity(child.pid)
                if (
                    known is None
                    or known["group"] != child.pid
                    or ready != {"phase_id": record["cpu_phase_id"], "identity": known}
                ):
                    raise RuntimeError("Inert cleanup worker identity differs")
                # Reload: prior attempt may have recovered a UUID/UID canonically.
                record = json.loads(ledger.read_bytes())
                record.setdefault("cleanup_workers", []).append(known)
                write_json(ledger, record)
                write_json(root / "authorization.json", ready)
                child.wait(timeout=max(0.01, deadline - time.time()))
                result = json.loads((root / "result.json").read_bytes())
                if result.get("phase_id") != record["cpu_phase_id"]:
                    raise RuntimeError("Cleanup result phase differs")
                if result.get("released") is not True:
                    raise RuntimeError(
                        "Cleanup attempt failed: " + result.get("error_type", "unknown")
                    )
                return result
            if child.poll() is not None:
                raise RuntimeError("Inert cleanup worker exited")
            time.sleep(0.05)
        raise TimeoutError("Cleanup attempt registration expired")
    finally:
        if known is None:
            # Popen still owns an unregistered inert child; no API was allowed.
            from profile_sharded_processes import identity, stop_group

            inert = identity(child.pid)
            if inert is not None:
                stop_group(inert)
            child.wait(timeout=2)
        else:
            stop_attempt(child, known)


def cleanup(ledger, *, recovery=False):
    import fcntl

    # One cleanup API owner; a dead supervisor releases this kernel lock.
    with (ledger.parent / "cleanup-owner.lock").open("a") as owner:
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another cleanup supervisor owns this phase") from error
        return _cleanup_locked(ledger, recovery=recovery)


def _cleanup_locked(ledger, *, recovery=False):
    from profile_sharded_plan import validate_ledger

    record = validate_ledger(ledger)
    if recovery and time.time() < record["cpu_deadline_epoch"]:
        raise RuntimeError("Release-only recovery is for expired phases")
    cleanup_deadline = time.time() + 120 if recovery else record["cpu_deadline_epoch"]
    if recovery:
        record.update(
            execution_failed=True,
            execution_passed=False,
            recovery_started_at=time.time(),
        )
        write_json(ledger, record)
    from profile_sharded_processes import stop_registered

    history_path = ledger.parent / "cleanup-history.json"
    if history_path.exists():
        retained = json.loads(history_path.read_bytes())
        if retained.get("phase_id") != record["cpu_phase_id"]:
            raise RuntimeError("Retained cleanup history phase differs")
        history = retained["errors"]
    else:
        history = []
    number = 0
    while time.time() < cleanup_deadline - 3:
        record = validate_ledger(ledger)
        # Continue numbered attempts after a manual cleanup continuation too.
        while (ledger.parent / ("cleanup-attempt-" + str(number))).exists():
            number += 1
        began = time.time()
        try:
            stop_registered(ledger)
            run_attempt(ledger, record, number, cleanup_deadline=cleanup_deadline)
        except Exception as error:
            history.append(
                {
                    "phase_id": record["cpu_phase_id"],
                    "attempt": number,
                    "started_at": began,
                    "finished_at": time.time(),
                    "error_type": type(error).__name__,
                }
            )
            write_json(
                ledger.parent / "cleanup-history.json",
                {
                    "phase_id": record["cpu_phase_id"],
                    "errors": history,
                    "released": False,
                },
            )
            if time.time() < cleanup_deadline - 3:
                time.sleep(min(1, max(0, cleanup_deadline - 3 - time.time())))
            number += 1
            continue
        record = json.loads(ledger.read_bytes())
        if recovery:
            record.update(
                status="failed_recovered", execution_failed=True, execution_passed=False
            )
            write_json(ledger, record)
        result = {
            "release_only_recovery": recovery,
            "execution_passed": False if recovery else None,
            "phase_id": record["cpu_phase_id"],
            "errors": history,
            "released": True,
            "finished_at": time.time(),
        }
        write_json(ledger.parent / "cleanup-history.json", result)
        return result
    record = json.loads(ledger.read_bytes())
    record["status"] = "cleanup_unconfirmed"
    write_json(ledger, record)
    write_json(
        ledger.parent / "cleanup-history.json",
        {
            "phase_id": record["cpu_phase_id"],
            "errors": history,
            "released": False,
            "deadline_reached": True,
            "finished_at": time.time(),
        },
    )
    raise RuntimeError(
        "Absolute600-second cleanup deadline reached; release remains unproved"
    )


def stop_launcher(record):
    from profile_sharded_processes import stop_group

    return stop_group(record["launcher_identity"])


def run_watchdog(ledger):
    guard = install(ledger)
    from chalkcompute._connect import ConnectClient
    import profile_host_guard as host

    record = json.loads(ledger.read_bytes())
    phase = record["cpu_phase_id"]
    client = ConnectClient()
    client._ensure_auth()
    if client._env_id != record["environment_id"]:
        raise RuntimeError("Watchdog environment differs")
    host.runtime_absence(None)
    probe = guard.ResourceGuard(
        record["name"],
        ledger.parent,
        record,
        record["kube_context"],
        record["fallback_kube_context"],
    )
    for context in probe.kube_contexts:
        for kind in ("chalkcontainers", "pods"):
            probe.kube_items(kind, name="synthetic-preflight-" + phase, context=context)
    write_json(
        ledger.parent / "cpu-watchdog-ready.json",
        {"phase_id": phase, "pid": os.getpid(), "ready_at": time.time()},
    )
    normal_request = False
    while time.time() < record["cpu_deadline_epoch"] - 120:
        record = json.loads(ledger.read_bytes())
        request = ledger.parent / "cpu-cleanup-request.json"
        if request.exists():
            value = json.loads(request.read_bytes())
            if (
                value.get("phase_id") != phase
                or value.get("controller_work_stopped") is not True
                or type(value.get("execution_passed")) is not bool
                or set(value)
                != {"phase_id", "controller_work_stopped", "execution_passed"}
            ):
                raise RuntimeError("Controller cleanup request phase differs")
            record.update(
                execution_passed=value["execution_passed"],
                execution_failed=not value["execution_passed"],
            )
            write_json(ledger, record)
            normal_request = True
            break
        if process_identity(record["launcher_pid"]) is None:
            break
        time.sleep(1)
    (ledger.parent / "cpu-expired").write_text(str(time.time()))
    if not normal_request:
        record.update(execution_failed=True, execution_passed=False)
        write_json(ledger, record)
        stop_launcher(record)
    try:
        cleanup(ledger)
    except Exception as error:
        write_json(
            ledger.parent / "cpu-watchdog-result.json",
            {
                "phase_id": phase,
                "released": False,
                "error_type": type(error).__name__,
                "finished_at": time.time(),
            },
        )
        raise
    write_json(
        ledger.parent / "cpu-watchdog-result.json",
        {
            "phase_id": phase,
            "record": json.loads(ledger.read_bytes()),
            "errors": [],
            "finished_at": time.time(),
        },
    )
    write_json(
        ledger.parent / "cpu-watchdog-exited.json",
        {"phase_id": phase, "exited_at": time.time(), "transferred": False},
    )


def arm_watchdog(guard, record, ledger):
    write_json(ledger, record)
    with (ledger.parent / "cpu-watchdog.log").open("ab", buffering=0) as log:
        watch = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--watchdog", str(ledger)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    caffeinate = subprocess.Popen(
        ["caffeinate", "-i", "-w", str(watch.pid)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    record.update(cpu_watchdog_pid=watch.pid, cpu_caffeinate_pid=caffeinate.pid)
    write_json(ledger, record)
    for _ in range(60):
        if watch.poll() is not None:
            raise RuntimeError("Synthetic watchdog exited before readiness")
        marker = ledger.parent / "cpu-watchdog-ready.json"
        if marker.exists():
            ready = json.loads(marker.read_bytes())
            if (
                ready.get("phase_id") != record["cpu_phase_id"]
                or ready.get("pid") != watch.pid
            ):
                raise RuntimeError("Synthetic watchdog phase/PID differs")
            return watch
        time.sleep(1)
    raise TimeoutError("Synthetic watchdog readiness expired; no allocation permitted")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watchdog", type=Path)
    parser.add_argument("--cleanup", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--attempt", type=Path)
    parser.add_argument("--recover-release", action="store_true")
    parser.add_argument("--fuse", type=Path)
    args = parser.parse_args()
    if args.fuse:
        from profile_sharded_processes import run_fuse

        run_fuse(args.fuse)
        raise SystemExit(0)
    if args.attempt:
        attempt_process(args.attempt)
        raise SystemExit(0)
    ledger = args.watchdog or args.cleanup
    if ledger is None:
        parser.error("Require watchdog or cleanup ledger")
    sys.path.insert(0, str(ledger.parent / "guard"))
    if args.env_file:
        from ftqa_smoke import load_env

        load_env(args.env_file)
    if args.watchdog:
        run_watchdog(ledger)
    else:
        from profile_sharded_plan import validate_ledger

        record = validate_ledger(ledger)
        (ledger.parent / "cpu-expired").write_text(str(time.time()))
        stop_launcher(record)
        cleanup(ledger, recovery=args.recover_release)
