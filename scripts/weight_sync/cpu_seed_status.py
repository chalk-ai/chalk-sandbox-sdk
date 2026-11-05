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
    for context in record["kube_contexts"]:
        try:
            containers = kube(
                context,
                "chalkcontainers",
                "metadata.name=cc-" + record["seed_sandbox_id"],
            )
            for container in containers:
                metadata = container["metadata"]
                owned = {
                    "context": context,
                    "name": metadata["name"],
                    "namespace": metadata["namespace"],
                    "uid": metadata["uid"],
                    "phase": container.get("status", {}).get("phase"),
                    "pods": [],
                }
                pods = kube(
                    context, "pods", "metadata.namespace=" + metadata["namespace"]
                )
                for pod in pods:
                    if not any(
                        owner.get("uid") == metadata["uid"]
                        for owner in pod["metadata"].get("ownerReferences", [])
                    ):
                        continue
                    state = pod.get("status", {})
                    owned["pods"].append(
                        {
                            "name": pod["metadata"]["name"],
                            "uid": pod["metadata"]["uid"],
                            "phase": state.get("phase"),
                            "states": [
                                {
                                    key: entry.get(key)
                                    for key in ("name", "ready", "state")
                                }
                                for entry in state.get("containerStatuses", [])
                            ],
                            "conditions": [
                                {
                                    key: entry.get(key)
                                    for key in ("type", "status", "reason")
                                }
                                for entry in state.get("conditions", [])
                            ],
                        }
                    )
                result["resources"].append(owned)
        except (subprocess.SubprocessError, OSError, ValueError) as error:
            result["errors"].append(
                {"context": context, "error_type": type(error).__name__}
            )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = inspect(args.ledger)
    text = json.dumps(result, indent=2)
    if args.out:
        args.out.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
