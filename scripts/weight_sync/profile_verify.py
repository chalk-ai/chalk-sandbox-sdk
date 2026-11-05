"""Independent read-only absence proof for a completed profiling CPU/GPU lease."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from profile_plan import write_json, validate, activate_controller_sdk
from profile_gpu_guard import ProfileGuard
from profile_rpc import lookup_group
from profile_writers import process_identity
import profile_host_guard as host


def verify(cpu_ledger, gpu_ledger, out):
    import chalkcompute as cc
    from chalkcompute._volume import VolumeNotFoundError

    cpu = json.loads(cpu_ledger.read_bytes())
    gpu = json.loads(gpu_ledger.read_bytes())
    if cpu["name"] != gpu["name"] or cpu["cpu_phase_id"] != gpu["cpu_seed_phase_id"]:
        raise RuntimeError("CPU/GPU ownership differs")
    out.mkdir(parents=True, exist_ok=False)
    guard = ProfileGuard(
        gpu["name"], out, gpu, gpu["kube_context"], gpu["fallback_kube_context"]
    )
    proof = {
        "cpu_phase_id": cpu["cpu_phase_id"],
        "gpu_phase_id": gpu["gpu_phase_id"],
        "checked_at": time.time(),
    }
    if (
        time.time()
        < max(gpu.get("submission_deadlines_epoch", {}).values(), default=0) + 60
    ):
        raise RuntimeError("GPU create reconciliation deadline has not elapsed")
    for suffix in ("-serve", "-fresh"):
        if lookup_group(gpu["name"] + suffix) is not None:
            raise RuntimeError("Owned serving group API association remains")
    for name, group_id in gpu.get("group_ids", {}).items():
        guard.verify_group_absent(name, group_id)
    for context in cpu["kube_contexts"]:
        if guard.kube_items(
            "chalkcontainers", name="cc-" + cpu["seed_sandbox_id"], context=context
        ):
            raise RuntimeError("CPU container CR remains")
        owners = {
            v["uid"] for v in cpu.get("seed_resources", []) if v["context"] == context
        }
        for pod in guard.kube_items("pods", context=context):
            if any(
                o.get("uid") in owners
                for o in pod["metadata"].get("ownerReferences", [])
            ):
                raise RuntimeError("CPU owner pod remains")
    proof["cpu_native_absence"] = host.runtime_absence(cpu["seed_sandbox_id"])
    absent = []
    for name in (cpu["name"], cpu["name"] + "-reference"):
        try:
            cc.Volume(name, create_if_missing=False).reload()
        except VolumeNotFoundError:
            absent.append(name)
        else:
            raise RuntimeError("Owned profiling volume remains")
    for record, keys in (
        (cpu, ("launcher_pid", "cpu_watchdog_pid", "cpu_caffeinate_pid")),
        (gpu, ("launcher_pid", "watchdog_pid", "sleep_inhibitor_pid")),
    ):
        for key in keys:
            if record.get(key) and process_identity(record[key]) is not None:
                raise RuntimeError("Owned launcher/watchdog/inhibitor remains: " + key)
    writer = gpu.get("pin_writer")
    if writer and process_identity(writer["pid"]) is not None:
        raise RuntimeError("PIN writer process remains")
    proof.update(
        owned_volumes_absent=absent,
        group_native_absence=gpu.get("profile_native_release_proofs", {}),
        owned_local_processes_absent=True,
        all_owned_resources_absent=True,
    )
    write_json(out / "cleanup-proof.json", proof)
    return proof


if __name__ == "__main__":
    from ftqa_smoke import load_env

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpu-ledger", type=Path, required=True)
    parser.add_argument("--gpu-ledger", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    validate(args.cpu_ledger.parent)
    activate_controller_sdk(args.cpu_ledger.parent)
    load_env(args.env_file)
    print(json.dumps(verify(args.cpu_ledger, args.gpu_ledger, args.out), indent=2))
