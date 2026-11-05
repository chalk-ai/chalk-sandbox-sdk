"""Fixed Oracle CPU HOST placement checks and independently enforced cleanup."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

HOST = "svlsbfe05ebc-oracle-a100"
SSH_HOST = "ubuntu@chalkaia100-1.tail0de09.ts.net"
POOL = "ExternalClusterHosts"
CONTEXT = "gke_chalk-serverless-stage_us-east4_chalk-svls-hwhz4h-us-east4-gke"


def membership() -> dict:
    result = subprocess.run(
        [
            "kubectl",
            "--context",
            CONTEXT,
            "get",
            "chalkhostpools.dataplane.chalk.ai",
            "external-cluster",
            "-n",
            "chalk-dataplane-controller",
            "-o",
            "json",
            "--request-timeout=15s",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    value = json.loads(result.stdout)
    capacity = value.get("status", {}).get("capacity") or {}
    from datetime import datetime

    def age(timestamp):
        if not isinstance(timestamp, str):
            raise ValueError("Missing external pool freshness timestamp")
        return (
            time.time()
            - datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
        )

    hosts = capacity.get("hosts") or []
    if (
        value.get("spec", {}).get("external") != {}
        or len(hosts) != 1
        or hosts[0].get("host_id") != HOST
        or hosts[0].get("accepting_placements") is not True
        or hosts[0].get("external_control_stream_connected") is not True
        or hosts[0].get("resources", {}).get("total", {}).get("gpus")
        != {"nvidia-a100-80gb": 8}
        or not 0 <= age(capacity.get("observed_at")) < 60
        or not 0 <= age(hosts[0].get("last_heartbeat_at")) < 60
    ):
        raise ValueError("Oracle external pool identity/membership/freshness changed")
    return {
        "host_id": HOST,
        "pool_id": POOL,
        "context": CONTEXT,
        "observed_at": capacity["observed_at"],
        "heartbeat_at": hosts[0]["last_heartbeat_at"],
        "singleton_pool": True,
    }


def ssh(args):
    return subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            SSH_HOST,
            shlex.join(args),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout


def require_placement(placements):
    if (
        len(placements) != 1
        or not isinstance(placements[0], dict)
        or placements[0].get("host_id") != HOST
        or placements[0].get("pool_id") != POOL
        or placements[0].get("state") != "Committed"
    ):
        raise RuntimeError("HOST placement differs; stop before accepting proof")
    return placements[0]


def runtime_identity(container_id):
    ids = {container_id, "cc-" + container_id}
    values = json.loads(
        ssh(
            [
                "sudo",
                "-n",
                "docker",
                "exec",
                "chalk-external-hypervisor",
                "runsc",
                "--root=/var/lib/chalk-hypervisor/runsc",
                "list",
                "--format=json",
            ]
        )
    )
    matches = [value for value in values if value.get("id") in ids]
    if len(matches) != 1:
        raise RuntimeError("Exact owned HOST runtime identity not present")
    return {key: matches[0].get(key) for key in ("id", "pid", "status")}


def runtime_absence(container_id: str | None) -> dict:
    if container_id is not None and (
        not container_id or any(c not in "0123456789abcdef-" for c in container_id)
    ):
        raise ValueError("Require the exact acknowledged owned sandbox UUID")
    ids = {container_id, "cc-" + container_id} if container_id else set()
    runtimes = json.loads(
        ssh(
            [
                "sudo",
                "-n",
                "docker",
                "exec",
                "chalk-external-hypervisor",
                "runsc",
                "--root=/var/lib/chalk-hypervisor/runsc",
                "list",
                "--format=json",
            ]
        )
    )
    if not isinstance(runtimes, list) or any(
        item.get("id") in ids for item in runtimes
    ):
        raise RuntimeError("Owned HOST runsc runtime remains or inventory invalid")
    # The external hypervisor has its own containerd socket, distinct from Docker's.
    pid = ssh(
        [
            "sudo",
            "-n",
            "docker",
            "inspect",
            "chalk-external-hypervisor",
            "--format",
            "{{.State.Pid}}",
        ]
    ).strip()
    if not pid.isdigit() or int(pid) <= 0:
        raise RuntimeError("Cannot identify external hypervisor containerd socket")
    prefix = [
        "sudo",
        "-n",
        "ctr",
        "--address",
        f"/proc/{pid}/root/run/containerd/containerd.sock",
    ]
    namespaces = ssh(prefix + ["namespaces", "list", "-q"]).splitlines()
    if not namespaces:
        raise RuntimeError("Cannot certify absence from an empty namespace inventory")
    for namespace in namespaces:
        tasks = ssh(
            prefix + ["--namespace", namespace, "tasks", "list", "-q"]
        ).splitlines()
        if any(task.strip() in ids for task in tasks):
            raise RuntimeError("Owned HOST containerd task remains")
    return {
        "host_id": HOST,
        "container_id": container_id,
        "runsc_absent": True if container_id else None,
        "containerd_task_absent": True if container_id else None,
        "inventory_access_only": container_id is None,
        "namespaces_checked": namespaces,
        "checked_at": time.time(),
    }


def install_release_check(guard):
    original = guard.release_cpu

    def release(record, out):
        original(record, out)
        # No disarm is permitted between API/CR cleanup and native HOST proof.
        record["cpu_compute_released"] = False
        guard.write_json(out / "launcher.json", record)
        if record.get("observed_host_id") not in (None, HOST):
            raise RuntimeError(
                "Unexpected HOST native cleanup requires its own host proof"
            )
        if record.get("seed_sandbox_id"):
            proof = runtime_absence(record["seed_sandbox_id"])
            guard.write_json(out / "host-runtime-absence.json", proof)
        elif record.get("submission_inflight"):
            raise RuntimeError("Ambiguous HOST submission must remain guarded")
        record["host_runtime_verified_absent"] = True
        record["cpu_compute_released"] = True
        guard.write_json(out / "launcher.json", record)

    guard.release_cpu = release


def arm_watchdog(guard, record, ledger):
    guard.write_json(ledger, record)
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
    guard.write_json(ledger, record)
    for _ in range(60):
        if watch.poll() is not None:
            raise RuntimeError("HOST watchdog exited before readiness")
        marker = ledger.parent / "cpu-watchdog-ready.json"
        if marker.exists():
            ready = json.loads(marker.read_bytes())
            if (
                ready.get("phase_id") != record["cpu_phase_id"]
                or ready.get("pid") != watch.pid
            ):
                raise RuntimeError("HOST watchdog phase/PID differs")
            return watch
        time.sleep(1)
    raise TimeoutError("HOST watchdog readiness timeout; no compute submitted")


def run_watchdog(guard, ledger):
    install_release_check(guard)
    proof = runtime_absence(None)
    guard.write_json(ledger.parent / "host-watchdog-inventory-ready.json", proof)
    # Existing fixed-lease watchdog dispatches our wrapped release on timeout,
    # launcher death and every ambiguous-create reconciliation attempt.
    guard.watchdog(ledger)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watchdog", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.watchdog.parent / "guard"))
    import cpu_seed

    run_watchdog(cpu_seed, args.watchdog)
