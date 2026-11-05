"""GPU profile cleanup: exact group owners, pods and fixed HOST native runtimes."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import profile_host_guard as host
from training_smoke import ResourceGuard
from profile_rpc import lookup_group, make_client
from profile_writers import stop_writer


def owned_runtime(name, group_id, replicas):
    if not re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", group_id):
        raise ValueError("Require exact acknowledged group UUID")
    if any(not item.startswith("sg-" + group_id + "-") for item in replicas):
        raise ValueError("Replica identity differs from acknowledged group UUID")
    return name in replicas or name.startswith("sg-" + group_id + "-")


def native_absence(group_id, replicas):
    values = json.loads(
        host.ssh(
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
    if not isinstance(values, list) or any(
        owned_runtime(v.get("id", ""), group_id, replicas) for v in values
    ):
        raise RuntimeError("Owned group native runsc runtime remains")
    pid = host.ssh(
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
        raise RuntimeError("Invalid hypervisor PID")
    prefix = [
        "sudo",
        "-n",
        "ctr",
        "--address",
        f"/proc/{pid}/root/run/containerd/containerd.sock",
    ]
    namespaces = host.ssh(prefix + ["namespaces", "list", "-q"]).splitlines()
    if not namespaces:
        raise RuntimeError("Empty containerd namespace inventory")
    for namespace in namespaces:
        tasks = host.ssh(
            prefix + ["--namespace", namespace, "tasks", "list", "-q"]
        ).splitlines()
        if any(owned_runtime(task.strip(), group_id, replicas) for task in tasks):
            raise RuntimeError("Owned group native containerd task remains")
    return {
        "group_id": group_id,
        "host_id": host.HOST,
        "runsc_absent": True,
        "containerd_tasks_absent": True,
        "checked_at": time.time(),
    }


class ProfileGuard(ResourceGuard):
    def capture_group(self, name, group_id):
        self.record.setdefault("group_ids", {})[name] = group_id
        for context in self.kube_contexts:
            items = self.kube_items(
                "chalkscalinggroups", name="sg-" + group_id, context=context
            )
            if not items:
                continue
            if (
                len(items) != 1
                or items[0]["metadata"]["namespace"]
                not in self.record["expected_namespaces"]
            ):
                raise RuntimeError("Ambiguous group owner/environment")
            owner = items[0]["metadata"]["uid"]
            children = self.kube_items("chalkcontainers", owner=owner, context=context)
            placements = [
                item.get("status", {}).get("host_placement") for item in children
            ]
            # During pending startup there can be no committed placement yet.
            for placement in placements:
                if placement is not None and placement.get("state") == "Committed":
                    host.require_placement([placement])
            value = {
                "id": group_id,
                "owners": [owner],
                "replicas": [v["metadata"]["name"] for v in children],
                "replica_uids": [v["metadata"]["uid"] for v in children],
                "context": context,
                "namespace": items[0]["metadata"]["namespace"],
            }
            prior = self.record.setdefault("profile_group_resources", {}).get(name)
            if prior and prior["owners"] != value["owners"]:
                raise RuntimeError("Scaling group owner UID changed")
            if prior:
                value["replicas"] = sorted(set(value["replicas"] + prior["replicas"]))
                value["replica_uids"] = sorted(
                    set(value["replica_uids"] + prior["replica_uids"])
                )
            self.record["profile_group_resources"][name] = value
            self.save()
            return value
        return self.record.get("profile_group_resources", {}).get(name)

    def verify_group_absent(self, name, group_id):
        value = self.capture_group(name, group_id)
        if value is None:
            raise RuntimeError(
                "No retained immutable group owner; cannot certify cleanup"
            )
        context = value["context"]
        crs = self.kube_items(
            "chalkscalinggroups", name="sg-" + group_id, context=context
        )
        children = [
            v
            for owner in value["owners"]
            for v in self.kube_items("chalkcontainers", owner=owner, context=context)
        ]
        owners = set(value["owners"] + value["replica_uids"])
        pods = [
            p
            for p in self.kube_items("pods", context=context)
            if p["metadata"].get("labels", {}).get("chalk.ai/scaling-group-owner")
            in owners
            or any(
                o.get("uid") in owners for o in p["metadata"].get("ownerReferences", [])
            )
        ]
        if crs or children or pods:
            raise RuntimeError("Owned CR/containers/pods still exist")
        proof = native_absence(group_id, value["replicas"])
        self.record.setdefault("profile_native_release_proofs", {})[group_id] = proof
        self.save()

    def stop_group(self, name, group):
        group_id = group.id or self.record["group_ids"][name]
        try:
            self.capture_group(name, group_id)
        except Exception:
            group.delete()
            raise
        super().stop_group(name, group)
        self.verify_group_absent(name, group_id)

    def _cleanup_compute(self):
        super()._cleanup_compute()
        # Check retained owners even if the API association vanished first.
        for name, group_id in self.record.get("group_ids", {}).items():
            self.verify_group_absent(name, group_id)


def cleanup_data(guard):
    stop_writer(guard.record, guard.out)
    import chalkcompute as cc
    from chalkcompute._volume import VolumeNotFoundError

    if guard.record.get("submission_inflight", {}).get("create_scaling_group"):
        until = (
            max(guard.record.get("submission_deadlines_epoch", {}).values(), default=0)
            + 60
        )
        if time.time() < until:
            return ["Ambiguous create still inside bounded reconciliation window"]
        for suffix in ("-serve", "-fresh"):
            if lookup_group(guard.name + suffix) is not None:
                return ["Ambiguous create produced an active owned group"]
        guard.record["submission_inflight"]["create_scaling_group"] = False
        guard.record["create_reconciled_absent_after_deadline"] = time.time()
        guard.save()
    for suffix in ("-serve", "-fresh"):
        if lookup_group(guard.name + suffix) is not None:
            return ["Owned serving group remains before volume cleanup"]
    if any(
        v not in guard.record.get("released_groups", [])
        or v not in guard.record.get("profile_native_release_proofs", {})
        for v in guard.record.get("group_ids", {}).values()
    ):
        return ["Owned native compute release is unconfirmed before volume cleanup"]
    errors = []
    for name in guard.record["owned_volume_names"]:
        try:
            cc.Volume(name, create_if_missing=False).delete()
            cc.Volume(name, create_if_missing=False).reload()
        except VolumeNotFoundError:
            continue
        except Exception as error:
            errors.append(type(error).__name__)
            continue
        errors.append("Volume deletion unconfirmed: " + name)
    return errors


def arm(guard):
    record = guard.record
    guard.claim_deadline = time.monotonic() + max(
        0, record["gpu_deadline_epoch"] - time.time()
    )
    guard.save()
    with (guard.out / "watchdog.log").open("ab", buffering=0) as log:
        watch = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--ledger",
                str(guard.out / "launcher.json"),
                "--deadline",
                str(record["gpu_deadline_epoch"]),
                "--parent-pid",
                str(os.getpid()),
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    record["watchdog_pid"] = watch.pid
    guard.watchdog_process = watch
    inhibitor = subprocess.Popen(
        ["caffeinate", "-i", "-w", str(watch.pid)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    record["sleep_inhibitor_pid"] = inhibitor.pid
    guard.save()
    for _ in range(60):
        if watch.poll() is not None:
            raise RuntimeError("Profile watchdog exited before readiness")
        if (guard.out / "watchdog-ready").exists():
            guard.check_budget()
            return watch
        time.sleep(0.5)
    raise TimeoutError("Profile watchdog failed to become ready; no GPU claim")


def watch(ledger, deadline, parent_pid):
    from cpu_seed import write_json

    client = make_client()
    client._ensure_auth()
    initial = json.loads(ledger.read_bytes())
    if client._env_id != initial["environment_id"]:
        raise RuntimeError("Profile watchdog environment differs")
    root = ledger.parent
    out = root / "watchdog-cleanup"
    out.mkdir(exist_ok=True)
    host.runtime_absence(None)  # Read-only native inventory access before readiness.
    phase = initial["gpu_phase_id"]
    write_json(
        root / "watchdog-ready",
        {"pid": os.getpid(), "phase_id": phase, "ready_at": time.time()},
    )
    observed = {}

    def snapshot():
        record = json.loads(ledger.read_bytes())
        if record["gpu_phase_id"] != phase:
            raise RuntimeError("Profile watchdog phase changed")
        for key in ("group_ids", "profile_group_resources"):
            for name, value in observed.get(key, {}).items():
                current = record.setdefault(key, {}).get(name)
                if current is None:
                    record[key][name] = value
                elif key == "group_ids" and current != value:
                    raise RuntimeError("Retained group UUID changed")
                elif key == "profile_group_resources":
                    if current["owners"] != value["owners"]:
                        raise RuntimeError("Retained owner UID changed")
                    for field in ("replicas", "replica_uids"):
                        current[field] = sorted(set(current[field] + value[field]))
        guard = ProfileGuard(
            record["name"],
            out,
            record,
            record["kube_context"],
            record["fallback_kube_context"],
        )
        for suffix in ("-serve", "-fresh"):
            name = record["name"] + suffix
            group = lookup_group(name, client)
            if group is not None:
                guard.groups.append((name, group))
                guard.capture_group(name, group.id)
        observed.update(
            {
                key: dict(record.get(key, {}))
                for key in ("group_ids", "profile_group_resources")
            }
        )
        write_json(out / "observed.json", observed)
        return guard

    while time.time() < deadline:
        if (root / "watchdog-disarmed").exists():
            marker = json.loads((root / "watchdog-disarmed").read_bytes())
            if marker.get("phase_id") != phase:
                raise RuntimeError("Wrong disarm phase")
            write_json(
                root / "watchdog-exited.json",
                {"phase_id": phase, "exited_at": time.time()},
            )
            return
        try:
            os.kill(parent_pid, 0)
        except ProcessLookupError:
            break
        try:
            snapshot()
        except Exception as error:
            write_json(
                out / "last-observation-error.json",
                {"type": type(error).__name__, "time": time.time()},
            )
        time.sleep(2)
    (root / "watchdog-expired").write_text(str(time.time()))
    history = []
    until = time.time()
    attempt = 0
    while True:
        attempt += 1
        errors = []
        try:
            guard = snapshot()
            until = max(
                until,
                max(
                    guard.record.get("submission_deadlines_epoch", {}).values(),
                    default=0,
                )
                + 60,
            )
            stop_writer(guard.record, root)
            guard.cleanup_compute()
        except Exception as error:
            errors.append(type(error).__name__)
        history.append({"attempt": attempt, "errors": errors, "at": time.time()})
        write_json(out / "cleanup-history.json", history)
        # Historical transient errors never suppress cleanup after a later proof.
        if not errors and time.time() >= until:
            for _ in range(6):
                data_errors = cleanup_data(guard)
                guard.record["cleanup_errors"] = data_errors
                guard.save()
                if not data_errors:
                    write_json(
                        root / "watchdog-exited.json",
                        {"phase_id": phase, "exited_at": time.time(), "cleaned": True},
                    )
                    return
                time.sleep(10)
        if attempt >= 6 and time.time() >= until:
            raise RuntimeError(
                "Profile cleanup unconfirmed; retained exact ownership evidence"
            )
        time.sleep(15)


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--deadline", type=float, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args()
    watch(args.ledger, args.deadline, args.parent_pid)


if __name__ == "__main__":
    main()
