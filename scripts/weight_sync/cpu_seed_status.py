"""Read-only CPU lease and exact UID-owned pod progress; never emits CR specs."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path


def kube(context, resource, selector):
    result = subprocess.run(
        [
            "kubectl",
            "--context",
            context,
            "get",
            resource,
            "-A",
            "--field-selector",
            selector,
            "-o",
            "json",
            "--request-timeout=20s",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)["items"]


def inspect(ledger):
    record = json.loads(ledger.read_text())
    result = {
        key: record.get(key)
        for key in (
            "name",
            "status",
            "seed_sandbox_id",
            "cpu_phase_id",
            "cpu_started_at",
            "cpu_deadline_epoch",
            "cpu_compute_released",
            "proof_sha256",
        )
    }
    result["lease_seconds_remaining"] = max(
        0, record["cpu_deadline_epoch"] - time.time()
    )
    result["resources"] = []
    result["errors"] = []
    if not record.get("seed_sandbox_id"):
        return result
    retained = {
        (item["context"], item["uid"]): item
        for item in record.get("seed_resources", [])
    }
    live = set()
    for context in record["kube_contexts"]:
        try:
            containers = kube(
                context,
                "chalkcontainers",
                "metadata.name=cc-" + record["seed_sandbox_id"],
            )
            for container in containers:
                metadata = container["metadata"]
                item = {
                    "context": context,
                    "name": metadata["name"],
                    "namespace": metadata["namespace"],
                    "uid": metadata["uid"],
                    "phase": container.get("status", {}).get("phase"),
                }
                retained[(context, metadata["uid"])] = item
                live.add((context, metadata["uid"]))
        except (subprocess.SubprocessError, OSError, ValueError) as error:
            result["errors"].append(
                {"context": context, "error_type": type(error).__name__}
            )
    for key, resource in retained.items():
        owned = {
            field: resource.get(field)
            for field in ("context", "name", "namespace", "uid", "phase")
        }
        owned["cr_present"] = key in live
        owned["pods"] = []
        try:
            pods = kube(
                resource["context"],
                "pods",
                "metadata.namespace=" + resource["namespace"],
            )
            for pod in pods:
                if not any(
                    owner.get("uid") == resource["uid"]
                    for owner in pod["metadata"].get("ownerReferences", [])
                ):
                    continue
                state = pod.get("status", {})
                states = []
                for entry in state.get("containerStatuses", []):
                    clean = {"name": entry["name"], "ready": entry.get("ready")}
                    for kind, value in entry.get("state", {}).items():
                        clean[kind] = {
                            field: value.get(field)
                            for field in (
                                "reason",
                                "exitCode",
                                "startedAt",
                                "finishedAt",
                            )
                            if field in value
                        }
                    states.append(clean)
                owned["pods"].append(
                    {
                        "name": pod["metadata"]["name"],
                        "uid": pod["metadata"]["uid"],
                        "phase": state.get("phase"),
                        "states": states,
                        "conditions": [
                            {
                                field: entry.get(field)
                                for field in ("type", "status", "reason")
                            }
                            for entry in state.get("conditions", [])
                        ],
                    }
                )
        except (subprocess.SubprocessError, OSError, ValueError) as error:
            result["errors"].append(
                {"context": resource["context"], "error_type": type(error).__name__}
            )
        result["resources"].append(owned)
    result["retained_owner_uid_count"] = len(record.get("seed_resources", []))
    result["owned_cr_and_uid_pods_absent"] = (
        bool(record.get("seed_resources"))
        and not result["errors"]
        and not any(item["cr_present"] or item["pods"] for item in result["resources"])
    )
    return result


GUEST_PROGRESS = r"""
import json,time
from pathlib import Path
files=[p for p in Path('/root/.cache/huggingface').rglob('*') if p.is_file() and not p.is_symlink()]
processes=[]
for folder in Path('/proc').glob('[0-9]*'):
    try:
        name=(folder/'comm').read_text().strip()
        if name.startswith('python'):
            fields={line.split(':',1)[0]:line.split(':',1)[1].strip() for line in (folder/'status').read_text().splitlines() if line.startswith(('VmRSS:','Threads:'))}
            processes.append({'pid':folder.name,'name':name,**fields})
    except OSError:
        pass
print(json.dumps({'time':time.time(),'cache_files':len(files),'cache_bytes':sum(p.stat().st_size for p in files),'python_processes':processes}))
"""


def guest_progress(ledger, env_file):
    import chalkcompute as cc
    from chalkcompute._connect import ConnectClient
    from ftqa_smoke import load_env

    record = json.loads(ledger.read_text())
    load_env(env_file)
    client = ConnectClient()
    client._ensure_auth()
    if client._env_id != record["environment_id"]:
        raise RuntimeError("Guest status authentication environment mismatch")
    try:
        result = cc.Sandbox.from_id(record["seed_sandbox_id"]).exec(
            "python3", "-c", GUEST_PROGRESS, timeout_secs=20
        )
        if result.exit_code == 0 and result.error is None:
            return json.loads(result.stdout_text)
        return {"exit_code": result.exit_code, "status": "unavailable"}
    except Exception as error:
        return {"status": "unavailable", "error_type": type(error).__name__}


def volume_absence(ledger, env_file):
    import chalkcompute as cc
    from chalkcompute._connect import ConnectClient
    from chalkcompute._volume import VolumeNotFoundError
    from ftqa_smoke import load_env

    record = json.loads(ledger.read_text())
    load_env(env_file)
    client = ConnectClient()
    client._ensure_auth()
    if client._env_id != record["environment_id"]:
        raise RuntimeError("Volume absence authentication environment mismatch")
    try:
        cc.Volume(record["name"], create_if_missing=False).reload()
    except VolumeNotFoundError:
        return {
            "name": record["name"],
            "absent": True,
            "exact_environment_verified": True,
        }
    return {"name": record["name"], "absent": False, "exact_environment_verified": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--guest", action="store_true")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--verify-volume-absence", action="store_true")
    args = parser.parse_args()
    result = inspect(args.ledger)
    if args.guest:
        if not args.env_file:
            parser.error("--guest requires --env-file")
        result["guest"] = guest_progress(args.ledger, args.env_file)
    if args.verify_volume_absence:
        if not args.env_file:
            parser.error("--verify-volume-absence requires --env-file")
        result["volume"] = volume_absence(args.ledger, args.env_file)
        result["all_owned_resources_released"] = (
            result.get("owned_cr_and_uid_pods_absent") and result["volume"]["absent"]
        )
    text = json.dumps(result, indent=2)
    if args.out:
        args.out.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
