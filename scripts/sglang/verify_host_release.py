"""Read-only independent CR/owner-UID pod/native/process release verification."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import time

import host_guard


def check_kubernetes(context, kind, container_id, owner_uids):
    command = ["kubectl", "--context", context, "get", kind, "-A", "-o", "json"]
    if kind == "chalkcontainers":
        command += ["--field-selector", "metadata.name=cc-" + container_id]
    result = subprocess.run(
        command, check=True, capture_output=True, text=True, timeout=30
    )
    items = json.loads(result.stdout)["items"]
    if kind == "pods":
        items = [
            item
            for item in items
            if any(
                owner.get("uid") in owner_uids
                for owner in item.get("metadata", {}).get("ownerReferences", [])
            )
        ]
    if items:
        raise RuntimeError(
            "Exact owned Kubernetes resources remain: " + context + "/" + kind
        )
    return {"context": context, "resource": kind, "owned_count": 0}


def verify(out):
    record = json.loads((out / "launcher.json").read_bytes())
    if (
        record.get("status") != "released"
        or record.get("observed_host_id") != host_guard.HOST
    ):
        raise RuntimeError("Require released exact known HOST phase")
    container_id = record["seed_sandbox_id"]
    owner_uids = {resource["uid"] for resource in record.get("seed_resources", [])}
    if not owner_uids:
        raise RuntimeError("Retained exact CR owner UID is required")
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(check_kubernetes, context, kind, container_id, owner_uids)
            for context in record["kube_contexts"]
            for kind in ("chalkcontainers", "pods")
        ]
        checks = [future.result() for future in futures]
    native = host_guard.runtime_absence(container_id)
    processes = {}
    for key in ("launcher_pid", "cpu_watchdog_pid", "cpu_caffeinate_pid"):
        try:
            os.kill(record[key], 0)
        except ProcessLookupError:
            processes[key] = "absent"
        else:
            raise RuntimeError("Owned controller process still exists: " + key)
    disarmed = json.loads((out / "cpu-watchdog-disarmed.json").read_bytes())
    exited = json.loads((out / "cpu-watchdog-exited.json").read_bytes())
    if any(value["phase_id"] != record["cpu_phase_id"] for value in (disarmed, exited)):
        raise RuntimeError("Watchdog proof phase differs")
    proof = {
        "verified_at": time.time(),
        "retained_cr_uids": sorted(owner_uids),
        "cr_and_owner_uid_pod_checks": checks,
        "native_host": native,
        "processes": processes,
        "owned_volume_names": record["owned_volume_names"],
        "watchdog_disarmed": disarmed,
        "watchdog_exited": exited,
    }
    if proof["owned_volume_names"]:
        import chalkcompute as cc
        from chalkcompute._volume import VolumeNotFoundError

        for name in proof["owned_volume_names"]:
            try:
                cc.Volume(name, create_if_missing=False).reload()
            except VolumeNotFoundError:
                continue
            raise RuntimeError("Owned fixture volume remains")
        proof["owned_volumes_verified_absent"] = True
    if record.get("upload_writer"):
        import gpu_guard

        if gpu_guard.process_identity(record["upload_writer"]["pid"]) is not None:
            raise RuntimeError("Upload writer remains after controller cleanup")
        proof["upload_writer_verified_absent"] = True
    target = out / "independent-cleanup-proof.json"
    with target.open("x") as handle:
        json.dump(proof, handle, indent=2)
    return proof


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    print(json.dumps(verify(parser.parse_args().out), indent=2))
