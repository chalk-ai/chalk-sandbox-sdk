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


def verify_volumes(record):
    import chalkcompute as cc
    from chalkcompute._volume import VolumeNotFoundError
    from chalkcompute._connect import ConnectClient

    client = ConnectClient()
    client._ensure_auth()
    if client._env_id != record["environment_id"]:
        raise RuntimeError(
            "Independent volume verifier authenticated environment differs"
        )
    volume_client = cc.VolumeClient.from_connect(client)

    for name in record["owned_volume_names"]:
        try:
            cc.Volume(name, client=volume_client, create_if_missing=False).reload()
        except VolumeNotFoundError:
            continue
        raise RuntimeError("Owned fixture volume remains")
    return client._env_id


def verify_api_association(record):
    """One exact authenticated lookup; never enumerate unrelated sandboxes."""
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError
    from chalkcompute._connect import ConnectClient
    from chalkcompute._gen.chalk.sandbox.v2 import service_connect, service_pb

    client = ConnectClient()
    client._ensure_auth()
    if client._env_id != record["environment_id"]:
        raise RuntimeError("Sandbox verifier authenticated environment differs")
    sandbox_id = record["seed_sandbox_id"]
    try:
        response = client.call(
            client.stub(service_connect.SandboxServiceClientSync).get_sandbox,
            service_pb.GetSandboxRequest(id=sandbox_id),
            timeout=15,
        )
    except ConnectError as error:
        if error.code != Code.NOT_FOUND:
            raise
        status = "not_found"
    else:
        info = response.sandbox
        if (
            info is None
            or info.id != sandbox_id
            or info.name != record.get("seed_name", record["name"])
        ):
            raise RuntimeError("Sandbox API association identity differs")
        if info.status != service_pb.SandboxStatus.TERMINATED:
            raise RuntimeError("Sandbox API association remains nonterminated")
        status = "terminated"
    return {
        "sandbox_id": sandbox_id,
        "status": status,
        "environment_id": client._env_id,
        "rpc_timeout_seconds": 15,
    }


def verify_reconciliation(record, out):
    """Reject early absence while an ambiguous create can still arrive."""
    if record.get("submission_inflight") is not False:
        raise RuntimeError("Sandbox submission is still ambiguous")
    proof = {"sandbox_submission_inflight": False}
    if "create_reconciled_until_epoch" in record:
        until = record["create_reconcile_until_epoch"]
        reconciled = record["create_reconciled_until_epoch"]
        if not (until <= reconciled <= time.time()):
            raise RuntimeError("Sandbox create reconciliation horizon not satisfied")
        proof["sandbox_reconciled_until_epoch"] = reconciled
    marker_path = out / "upload-create.json"
    if marker_path.exists():
        marker = json.loads(marker_path.read_bytes())
        if (
            marker["phase_id"] != record["cpu_phase_id"]
            or marker["name"] not in record["owned_volume_names"]
            or marker["rpc_deadline_epoch"] != marker["started_at"] + 30
            or marker["reconcile_until_epoch"] != marker["started_at"] + 90
            or marker["status"] not in {"inflight", "acknowledged"}
        ):
            raise RuntimeError("Fixture create marker identity/deadlines differ")
        if marker["status"] == "inflight":
            until = marker["reconcile_until_epoch"]
            reconciled = record.get("fixture_create_reconciled_until_epoch", 0)
            if not (until <= reconciled <= time.time()):
                raise RuntimeError(
                    "Fixture create reconciliation horizon not satisfied"
                )
            proof["fixture_reconciled_until_epoch"] = reconciled
        proof["fixture_create_status"] = marker["status"]
    elif record["owned_volume_names"]:
        raise RuntimeError("Owned fixture lacks its create ownership marker")
    return proof


def verify(out, *, proof_name=None):
    if proof_name is not None and (
        Path(proof_name).name != proof_name or not proof_name.endswith(".json")
    ):
        raise ValueError("proof_name must be a JSON filename without directories")
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
    reconciliation = verify_reconciliation(record, out)
    association = verify_api_association(record)
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
        "reconciliation": reconciliation,
        "sandbox_api_association": association,
    }
    if proof["owned_volume_names"]:
        proof["authenticated_environment_id"] = verify_volumes(record)
        proof["owned_volumes_verified_absent"] = True
    if record.get("upload_writer"):
        import gpu_guard

        if gpu_guard.process_identity(record["upload_writer"]["pid"]) is not None:
            raise RuntimeError("Upload writer remains after controller cleanup")
        proof["upload_writer_verified_absent"] = True
    target = out / (
        proof_name
        or (
            "independent-gpu-cleanup-proof.json"
            if record.get("gpu_claimed")
            else "independent-cleanup-proof.json"
        )
    )
    with target.open("x") as handle:
        json.dump(proof, handle, indent=2)
    return proof


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument(
        "--proof-name", help="Distinct JSON basename; never overwrites evidence"
    )
    args = parser.parse_args()
    if args.env_file:
        import sys

        sys.path.insert(0, str(args.out / "guard"))
        from ftqa_smoke import load_env

        load_env(args.env_file)
    print(json.dumps(verify(args.out, proof_name=args.proof_name), indent=2))
