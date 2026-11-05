"""Detached cleanup process: survives launcher death; never claims compute."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--deadline", type=float, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args()
    import chalkcompute as cc
    from chalkcompute._connect import ConnectClient
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError

    from training_smoke import ResourceGuard, cleanup_data, retain_trainer_evidence

    record = json.loads(args.ledger.read_text())
    out = args.ledger.parent / "watchdog-cleanup"
    out.mkdir(exist_ok=True)
    guard = ResourceGuard(
        record["name"],
        out,
        record,
        record["kube_context"],
        record["fallback_kube_context"],
    )
    client = ConnectClient()
    client._ensure_auth()
    if client._env_id != record["environment_id"]:
        raise RuntimeError("Watchdog authentication environment mismatch")
    guard.kube_items("chalkscalinggroups", name="watchdog-preflight-" + record["name"])
    guard.kube_items("chalkcontainers", owner="watchdog-preflight-" + record["name"])
    (args.ledger.parent / "watchdog-ready").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "ready_at": time.time(),
                "phase_id": record.get("gpu_phase_id"),
            }
        )
    )
    disarmed = args.ledger.parent / "watchdog-disarmed"
    while time.time() < args.deadline:
        record = json.loads(args.ledger.read_text())
        guard.record = record
        guard.run_id = record.get("training_run_id")
        resource_ledger = out / "training-resources.json"
        if resource_ledger.exists():
            record["training_resources"] = json.loads(resource_ledger.read_text())
        try:
            guard.capture_training_resource()
        except Exception as error:
            record.setdefault("observation_errors", []).append(
                f"{type(error).__name__}: {error}"
            )
            guard.save()
        if record.get("training_resources"):
            resource_ledger.write_text(
                json.dumps(record["training_resources"], indent=2)
            )
        if disarmed.exists():
            return
        try:
            os.kill(args.parent_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(2)
    if disarmed.exists():
        return
    try:
        (args.ledger.parent / "watchdog-expired").write_text(str(time.time()))
    except OSError:
        pass  # A full evidence disk must never prevent compute termination.
    # Reconcile repeatedly across an ambiguous in-flight create RPC. This never
    # submits or recreates resources; only exact unique names from the ledger.
    errors = []
    for attempt in range(6):
        record = json.loads(args.ledger.read_text())
        resource_ledger = out / "training-resources.json"
        if resource_ledger.exists():
            record["training_resources"] = json.loads(resource_ledger.read_text())
        guard = ResourceGuard(
            record["name"],
            out,
            record,
            record["kube_context"],
            record["fallback_kube_context"],
        )
        for suffix in ("-serve", "-fresh"):
            name = record["name"] + suffix
            try:
                group = cc.ScalingGroup.from_name(name)
            except Exception as error:
                cause = error if isinstance(error, ConnectError) else error.__cause__
                if isinstance(cause, ConnectError) and cause.code == Code.NOT_FOUND:
                    continue
                errors.append(f"{name}: {type(error).__name__}: {error}")
            else:
                guard.groups.append((name, group))
        try:
            guard.cleanup_compute()
        except Exception as error:
            errors.append(f"attempt {attempt}: {type(error).__name__}: {error}")
        if attempt != 5:
            time.sleep(15)
    (out / "result.json").write_text(
        json.dumps(
            {"cleanup_attempts": 6, "errors": errors, "finished_at": time.time()},
            indent=2,
        )
    )
    if errors:
        raise RuntimeError(
            "Watchdog cleanup had errors; inspect watchdog-cleanup evidence"
        )
    retain_trainer_evidence(guard)
    data_errors = cleanup_data(guard)
    guard.record["cleanup_errors"] = data_errors
    guard.save()
    if data_errors:
        raise RuntimeError("Watchdog data cleanup incomplete")


if __name__ == "__main__":
    main()
