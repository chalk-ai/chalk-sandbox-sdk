"""Independent placement-specific cleanup, with an append-only owner ledger."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path


def main():
    import chalkcompute as cc
    from chalkcompute._connect import ConnectClient
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError

    from training_smoke import ResourceGuard, cleanup_data, retain_trainer_evidence

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--deadline", type=float, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args()
    out = args.ledger.parent / "placement-watchdog"
    out.mkdir(exist_ok=True)
    owners = (
        json.loads((out / "owners.json").read_text())
        if (out / "owners.json").exists()
        else {}
    )

    def snapshot():
        record = json.loads(args.ledger.read_text())
        record["kube_contexts"] = list(
            dict.fromkeys(record.get("kube_contexts", []) + [args.context])
        )
        record["kube_context"] = args.context
        guard = ResourceGuard(record["name"], out, record, args.context, args.context)
        for name, group_id in record.get("group_ids", {}).items():
            if name in owners:
                continue
            crs = guard.kube_items("chalkscalinggroups", name="sg-" + group_id)
            if crs:
                assert len(crs) == 1
                metadata = crs[0]["metadata"]
                assert metadata["namespace"] in record["expected_namespaces"]
                owners[name] = {
                    "id": group_id,
                    "uid": metadata["uid"],
                    "namespace": metadata["namespace"],
                    "context": args.context,
                }
                (out / "owners.json").write_text(json.dumps(owners, indent=2))
        training_ledger = out / "training-resources.json"
        if training_ledger.exists():
            record["training_resources"] = json.loads(training_ledger.read_text())
        guard.capture_training_resource()
        if record.get("training_resources"):
            training_ledger.write_text(
                json.dumps(record["training_resources"], indent=2)
            )
        return guard

    client = ConnectClient()
    client._ensure_auth()
    guard = snapshot()
    assert client._env_id == guard.record["environment_id"]
    assert owners, "No actual SG owner UID captured"
    (out / "ready").write_text(str(os.getpid()))
    while time.time() < args.deadline:
        snapshot()
        if (args.ledger.parent / "watchdog-disarmed").exists():
            break
        try:
            os.kill(args.parent_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(2)
    guard = snapshot()
    errors = []
    try:
        guard.stop_training()
    except Exception as error:
        errors.append(f"training: {type(error).__name__}: {error}")
    for name, group_id in guard.record.get("group_ids", {}).items():
        try:
            try:
                group = cc.ScalingGroup.from_name(name)
            except Exception as error:
                cause = error if isinstance(error, ConnectError) else error.__cause__
                if not isinstance(cause, ConnectError) or cause.code != Code.NOT_FOUND:
                    raise
            else:
                guard.stop_group(name, group)
            owner = owners.get(name)
            assert owner, f"No owner proof captured for {name}"
            for _ in range(36):
                crs = guard.kube_items("chalkscalinggroups", name="sg-" + group_id)
                children = guard.kube_items("chalkcontainers", owner=owner["uid"])
                if not crs and not children:
                    guard.record.setdefault("verified_absent_groups", []).append(
                        group_id
                    )
                    break
                time.sleep(5)
            else:
                raise RuntimeError(f"Owned CR/replicas remain for {group_id}")
        except Exception as error:
            errors.append(f"{name}: {type(error).__name__}: {error}")
    if not errors:
        retain_trainer_evidence(guard)
        errors.extend(cleanup_data(guard))
    guard.record["cleanup_errors"] = errors
    guard.record["finished_at"] = time.time()
    guard.save()
    if errors:
        raise RuntimeError("Placement watchdog cleanup incomplete")


if __name__ == "__main__":
    main()
