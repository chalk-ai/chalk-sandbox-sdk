"""Independent read-only CPU-only or CPU/GPU absence proof, including no claim."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from profile_plan import write_json, validate, activate_controller_sdk
from profile_gpu_guard import ProfileGuard
from profile_rpc import lookup_group
from profile_writers import process_identity
from profile_volumes import marker_path, RPC_SECONDS, RECONCILE_SECONDS
import profile_host_guard as host


def volume_ownership(cpu, root, now):
    owned, collisions = [], []
    for name in (cpu["name"], cpu["name"] + "-reference"):
        path = marker_path(root, name)
        if not path.exists():
            continue  # No metadata create was permitted for this name.
        marker = json.loads(path.read_bytes())
        if (
            marker.get("phase_id") != cpu["cpu_phase_id"]
            or marker.get("name") != name
            or marker.get("rpc_deadline_epoch") != marker["started_at"] + RPC_SECONDS
            or marker.get("reconcile_until_epoch")
            != marker["started_at"] + RECONCILE_SECONDS
        ):
            raise RuntimeError("CPU metadata ownership/deadline marker differs")
        if marker["status"] == "collision":
            collisions.append(name)
        elif marker["status"] in ("inflight", "acknowledged"):
            if marker["status"] == "inflight" and now < marker["reconcile_until_epoch"]:
                raise RuntimeError("CPU metadata late-create reconciliation still open")
            owned.append(name)
        else:
            raise RuntimeError("Unknown CPU metadata ownership state")
    return owned, collisions


def require_local_exit(record, keys):
    for key in keys:
        if record.get(key) and process_identity(record[key]) is not None:
            raise RuntimeError("Owned launcher/watchdog/inhibitor remains: " + key)


def sandbox_status(sandbox_id):
    """Exact15s non-retrying GetSandbox; failed/error alone never proves release."""
    from profile_cpu_lifecycle import sandbox_rpc

    result = sandbox_rpc(sandbox_id)
    if result["status"] not in ("terminated", "not_found"):
        raise RuntimeError("Exact CPU sandbox is not TERMINATED/NOT_FOUND")
    return result


def verify(cpu_ledger, gpu_ledger, out):
    import chalkcompute as cc
    from chalkcompute._connect import ConnectClient
    from chalkcompute._volume import VolumeNotFoundError

    cpu = json.loads(cpu_ledger.read_bytes())
    gpu = json.loads(gpu_ledger.read_bytes()) if gpu_ledger is not None else None
    if gpu is not None and (
        cpu["name"] != gpu["name"] or cpu["cpu_phase_id"] != gpu["cpu_seed_phase_id"]
    ):
        raise RuntimeError("CPU/GPU ownership differs")
    client = ConnectClient()
    client._ensure_auth()
    if client._env_id != cpu["environment_id"]:
        raise RuntimeError("Independent cleanup environment differs")
    now = time.time()
    owned, collisions = volume_ownership(cpu, cpu_ledger.parent, now)
    require_local_exit(cpu, ("launcher_pid", "cpu_watchdog_pid", "cpu_caffeinate_pid"))
    if now < cpu.get("create_reconcile_until_epoch", 0):
        raise RuntimeError("CPU compute late-create reconciliation still open")
    out.mkdir(parents=True, exist_ok=False)
    record = gpu if gpu is not None else cpu
    guard = ProfileGuard(
        record["name"],
        out,
        record,
        record["kube_context"],
        record["fallback_kube_context"],
    )
    proof = {
        "cpu_phase_id": cpu["cpu_phase_id"],
        "checked_at": now,
        "mode": "cpu_gpu" if gpu else "cpu_only",
    }
    if gpu is not None:
        proof["gpu_phase_id"] = gpu["gpu_phase_id"]
        if (
            now
            < max(gpu.get("submission_deadlines_epoch", {}).values(), default=0) + 60
        ):
            raise RuntimeError("GPU create reconciliation deadline has not elapsed")
        require_local_exit(gpu, ("launcher_pid", "watchdog_pid", "sleep_inhibitor_pid"))
        if (
            gpu.get("pin_writer")
            and process_identity(gpu["pin_writer"]["pid"]) is not None
        ):
            raise RuntimeError("PIN writer process remains")
        for suffix in ("-serve", "-fresh"):
            if lookup_group(gpu["name"] + suffix) is not None:
                raise RuntimeError("Owned serving group API association remains")
        # Always query fresh CR/pod/runsc/containerd state, not just ledger booleans.
        for name, group_id in gpu.get("group_ids", {}).items():
            guard.verify_group_absent(name, group_id)
        proof["group_native_absence"] = gpu.get("profile_native_release_proofs", {})
    ids = {cpu["seed_sandbox_id"]} if cpu.get("seed_sandbox_id") else set()
    if not ids and cpu.get("create_rpc_deadline_epoch"):
        # API ListSandboxes has no server-side exact name filter. Broad listing
        # is both unbounded and insufficient for ambiguous-create ownership.
        raise RuntimeError(
            "Unacknowledged CPU UUID unavailable; bounded exact-name API lookup is unsupported"
        )
    api = [sandbox_status(id) for id in sorted(ids)]
    proof["cpu_api_terminal_matches"] = api
    for context in cpu["kube_contexts"]:
        resources = guard.kube_items("chalkcontainers", context=context)
        if any(
            v["metadata"]["name"] in {"cc-" + id for id in ids}
            or v.get("metadata", {}).get("labels", {}).get("chalk.ai/container-name")
            == cpu["seed_name"]
            for v in resources
        ):
            raise RuntimeError("CPU container CR remains")
        owners = {
            v["uid"] for v in cpu.get("seed_resources", []) if v["context"] == context
        }
        for pod in guard.kube_items("pods", context=context):
            meta = pod["metadata"]
            labels = meta.get("labels", {})
            if (
                labels.get("chalk.ai/container") in ids
                or labels.get("chalk.ai/container-name") == cpu["seed_name"]
                or any(o.get("uid") in owners for o in meta.get("ownerReferences", []))
            ):
                raise RuntimeError("CPU UUID/name/retained-owner pod remains")
    if ids:
        proof["cpu_native_absence"] = [host.runtime_absence(id) for id in sorted(ids)]
    else:
        proof["cpu_native_absence"] = {
            "never_submitted": True,
            "no_create_rpc_marker": True,
        }
    absent = []
    for name in owned:
        try:
            cc.Volume(name, create_if_missing=False).reload()
        except VolumeNotFoundError:
            absent.append(name)
        else:
            raise RuntimeError("Owned profiling volume remains")
    proof.update(
        owned_volumes_absent=absent,
        preexisting_collisions_not_owned=collisions,
        owned_local_processes_absent=True,
        all_owned_resources_absent=True,
    )
    write_json(out / "cleanup-proof.json", proof)
    return proof


if __name__ == "__main__":
    from ftqa_smoke import load_env

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpu-ledger", type=Path, required=True)
    parser.add_argument("--gpu-ledger", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    validate(args.cpu_ledger.parent)
    activate_controller_sdk(args.cpu_ledger.parent)
    load_env(args.env_file)
    print(json.dumps(verify(args.cpu_ledger, args.gpu_ledger, args.out), indent=2))
