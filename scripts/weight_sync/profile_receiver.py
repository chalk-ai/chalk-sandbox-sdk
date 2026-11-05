"""Proof-bound one-A100 profile/restore/fresh receiver; execution needs root approval."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import time
import uuid
from pathlib import Path
from typing import Any

from profile_gpu_guard import ProfileGuard, arm, cleanup_data
from profile_gpu_payload import (
    native_audit,
    profile_events,
    serving_program,
    validate_cpu_proof,
    validate_profile_events,
    require_zero_failures,
)
from profile_plan import require_authorization, sha, write_json
from profile_rpc import make_client
from profile_writers import write_pin, stop_writer
import profile_host_guard as host


def cpu_lease(plan_out, plan):
    record = json.loads((plan_out / "launcher.json").read_bytes())
    payload = (plan_out / "cpu-proof.json").read_bytes()
    if (
        hashlib.sha256(payload).hexdigest() != record["proof_sha256"]
        or record["plan_sha256"] != sha(plan_out / "plan.json")
        or record["cpu_deadline_epoch"] <= time.time()
    ):
        raise ValueError("CPU proof/plan changed or input lease expired")
    return record, validate_cpu_proof(record, json.loads(payload), plan)


def validate_transfer(cpu, record, authorization, exited):
    from cpu_seed import valid_handoff

    if not (
        valid_handoff(cpu, authorization)
        and authorization.get("gpu_phase_id") == record["gpu_phase_id"]
        and authorization.get("gpu_deadline_epoch") == record["gpu_deadline_epoch"]
        and authorization.get("execution_config_sha256")
        == record["execution_config_sha256"]
        and authorization.get("plan_sha256") == record["plan_sha256"]
        and exited.get("phase_id") == cpu["cpu_phase_id"]
        and exited.get("transferred") is True
    ):
        raise ValueError(
            "CPU transfer does not bind this exact GPU receiver/config/deadline"
        )


def revalidate_snapshots(cpu, proof):
    import chalkcompute as cc
    from chalkcompute._weights_format import read_pointer, version_dir

    for name, snapshot_id, manifests, current, pin in (
        (cpu["name"], proof["snapshot_id"], proof["manifests_sha256"], 1, 0),
        (
            proof["reference_volume_name"],
            proof["reference_snapshot_id"],
            {"0": proof["reference_manifest_sha256"]},
            0,
            None,
        ),
    ):
        volume = cc.Volume(name, create_if_missing=False)
        volume.reload()
        if volume.version.id != snapshot_id:
            raise RuntimeError("CPU-proven immutable volume changed")
        snapshot = volume.at_version(snapshot_id)
        if read_pointer(snapshot.read_file("CURRENT")) != current:
            raise RuntimeError("CPU-proven CURRENT changed")
        if pin is not None and read_pointer(snapshot.read_file("PIN")) != pin:
            raise RuntimeError("CPU-proven PIN changed")
        for version, digest in manifests.items():
            payload = snapshot.read_file(version_dir(int(version)) + "/manifest.json")
            if hashlib.sha256(payload).hexdigest() != digest:
                raise RuntimeError("CPU-proven manifest changed")


def completion(group):
    response = group.call(
        "/v1/completions",
        json={
            "model": "policy",
            "prompt": "The capital of France is",
            "temperature": 0,
            "seed": 0,
            "max_tokens": 16,
            "ignore_eos": True,
        },
        timeout=30,
    )
    if response.status != 200:
        raise RuntimeError("Routed completion failed: " + str(response.status))
    return {
        "output": json.loads(response.content)["choices"][0]["text"],
        "version": response.headers["x-chalk-policy-version"],
    }


def wait_version(group, guard, target, seconds=180):
    deadline = min(time.time() + seconds, guard.record["gpu_deadline_epoch"] - 180)
    samples = []
    while time.time() < deadline:
        guard.check_budget()
        response = group.call("/chalk/health", method="GET", timeout=10)
        payload = json.loads(response.content)
        if response.status != 200 or payload.get("healthy") is not True:
            raise RuntimeError("Serving health lost")
        samples.append(
            {"at": time.time(), "applied_version": payload.get("applied_version")}
        )
        if payload.get("applied_version") == target:
            return samples
        time.sleep(0.2)
    raise TimeoutError(
        "Serving target version not visible before stage/cleanup reserve"
    )


def identity(group, guard):
    from training_smoke import native_processes, serving_replica_identities

    replicas = serving_replica_identities(group, guard)
    resource = guard.capture_group(group._name, group.id)
    if (
        not resource
        or len(resource["replicas"]) != 1
        or resource["replicas"][0] != replicas[0][2]
    ):
        raise RuntimeError("Replica owner identity differs")
    # Require committed singleton HOST placement before native access/proof.
    children = guard.kube_items(
        "chalkcontainers", owner=replicas[0][1], context=replicas[0][0]
    )
    host.require_placement(
        [c.get("status", {}).get("host_placement") for c in children]
    )
    return {
        "replicas": replicas,
        "native": native_processes(replicas[0][2], host.SSH_HOST),
    }


def execute(plan_out, out, authorization_path, env_file):
    from ftqa_smoke import load_env
    from seed_handoff import reserve_cpu_receiver

    began = time.time()
    authorization = json.loads(authorization_path.read_bytes())
    plan = require_authorization(plan_out, authorization, "execute_profile_gpu")
    if sha(Path(__file__)) != plan["guard_sources"]["profile_receiver.py"]:
        raise ValueError("Running GPU receiver differs from frozen source")
    cpu, proof = cpu_lease(plan_out, plan)
    if authorization.get("cpu_proof_sha256") != cpu["proof_sha256"]:
        raise ValueError("GPU approval must bind the actual completed CPU proof")
    out.mkdir(parents=True, exist_ok=False)
    load_env(env_file)
    import chalkcompute as cc

    record: dict[str, Any] = {
        "name": cpu["name"],
        "launcher_pid": os.getpid(),
        "status": "receiver_preparing",
        "environment_id": cpu["environment_id"],
        "expected_namespaces": cpu["expected_namespaces"],
        "kube_context": cpu["kube_context"],
        "fallback_kube_context": cpu["fallback_kube_context"],
        "kube_contexts": cpu["kube_contexts"],
        "owned_volume_names": cpu["owned_volume_names"],
        "gpu_phase_id": uuid.uuid4().hex,
        "gpu_budget_started_at": began,
        "gpu_deadline_epoch": began + 1800,
        "cpu_seed_phase_id": cpu["cpu_phase_id"],
        "cpu_seed_ledger": str(plan_out / "launcher.json"),
        "plan_sha256": sha(plan_out / "plan.json"),
        "cpu_proof_sha256": cpu["proof_sha256"],
        "owns_source_volume": True,
        "max_a100": 1,
        "max_amd": 0,
        "execution_config": {
            "image_uri": plan["image_uri"],
            "overlay_sha256": plan["overlay_sha256"],
            "profile_sources": plan["guard_sources"],
            "cpu_phase": cpu["cpu_phase_id"],
            "cpu_proof_sha256": cpu["proof_sha256"],
            "reference_snapshot": proof["reference_snapshot_id"],
            "startup_ready_seconds": 420,
            "update_seconds": 180,
            "restore_seconds": 180,
            "fresh_required_remaining_seconds": 720,
            "cleanup_reserve_seconds": 180,
            "budget_seconds": 1800,
        },
    }
    record["execution_config_sha256"] = hashlib.sha256(
        json.dumps(
            record["execution_config"], sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    record["cpu_receiver_claim"] = reserve_cpu_receiver(
        plan_out / "launcher.json", record, out
    )
    guard = ProfileGuard(
        record["name"],
        out,
        record,
        record["kube_context"],
        record["fallback_kube_context"],
    )
    client = make_client(guard)
    client._ensure_auth()
    if client._env_id != record["environment_id"]:
        raise RuntimeError("GPU environment differs from CPU proof")
    watch = arm(guard)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        write_json(
            out / "gpu-handoff-ready.json",
            {
                "gpu_phase_id": record["gpu_phase_id"],
                "gpu_deadline_epoch": record["gpu_deadline_epoch"],
                "gpu_guard_ready": True,
                "execution_config_sha256": record["execution_config_sha256"],
                "plan_sha256": record["plan_sha256"],
                "cpu_phase_id": cpu["cpu_phase_id"],
                "volume_name": cpu["name"],
                "hf_revision": cpu["hf_revision"],
                "prepared_image_sha256": cpu["prepared_image_sha256"],
                "source_overlay_sha256": cpu["source_overlay_sha256"],
                "training_deploy_image_uri": cpu["training_deploy_image_uri"],
                "proof_sha256": cpu["proof_sha256"],
                "snapshot_id": cpu["snapshot_id"],
                "reference_snapshot_id": proof["reference_snapshot_id"],
                "owns_transferred_source_volume": True,
            },
        )
        while time.time() < cpu["cpu_deadline_epoch"]:
            guard.check_budget()
            auth = plan_out / "cpu-authorization.json"
            exited = plan_out / "cpu-watchdog-exited.json"
            latest = json.loads((plan_out / "launcher.json").read_bytes())
            if (
                auth.exists()
                and exited.exists()
                and latest["status"] == "data_ownership_transferred"
            ):
                validate_transfer(
                    cpu,
                    record,
                    json.loads(auth.read_bytes()),
                    json.loads(exited.read_bytes()),
                )
                if latest.get("gpu_phase_id") != record["gpu_phase_id"]:
                    raise RuntimeError("CPU receiver ownership changed")
                revalidate_snapshots(cpu, proof)
                record["data_transfer_verified"] = True
                guard.save()
                break
            time.sleep(0.2)
        else:
            raise TimeoutError("CPU input lease expired before GPU transfer")
        write_json(out / "host-membership-before-gpu.json", host.membership())
        overlay = (plan_out / "runtime.tar.gz").read_bytes()
        program = serving_program(overlay)
        if len(program.encode()) > 120 * 1024:
            raise ValueError("GPU entrypoint exceeds safe argument bound")
        (out / "gpu-entrypoint.py").write_text(program)
        record["gpu_entrypoint_sha256"] = sha(out / "gpu-entrypoint.py")
        guard.save()

        def deploy(suffix, reference=False):
            guard.check_budget()
            if record["gpu_deadline_epoch"] - time.time() < 720:
                raise TimeoutError("Insufficient lease for startup/checks/cleanup")
            host.membership()
            env = {
                "PYTHONUNBUFFERED": "1",
                "OMP_NUM_THREADS": "16",
                "MKL_NUM_THREADS": "16",
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "CHALK_WEIGHT_SYNC_PROFILE": "1",
                "CHALK_WEIGHT_SYNC_POLL_S": ".2",
                "CHALK_WEIGHT_PROFILE_GPU_PHASE": record["gpu_phase_id"],
            }
            if reference:
                mount = cc.VolumeMount(
                    name=proof["reference_volume_name"],
                    mount_path="/chalk/weights",
                    version_id=proof["reference_snapshot_id"],
                )
            else:
                mount = cc.VolumeMount(
                    name=record["name"], mount_path="/chalk/weights", ref_name="main"
                )
            group = cc.ScalingGroup(
                name=record["name"] + suffix,
                image=plan["image_uri"],
                entrypoint=["python3", "-u", "-c", program],
                gpu="nvidia-a100-80gb:1",
                cpu="16",
                memory="128Gi",
                compute_class=cc.ComputeClass.HOST,
                execution_target=cc.ExecutionBackend.SERVERLESS,
                env=env,
                volumes=[mount],
                port=8000,
                min_replicas=1,
                max_replicas=1,
                authenticated=True,
                chalk_identity=False,
                readiness_probe=cc.ReadinessProbe.http("/chalk/health"),
            )
            group._client = client
            guard.groups.append((record["name"] + suffix, group))
            group.deploy(ready_timeout=420)
            return group

        live = deploy("-serve")
        before = identity(live, guard)
        replica = before["replicas"][0][2]
        baseline = completion(live)
        if baseline["version"] != "0":
            raise RuntimeError("Baseline did not load pinned source0")
        record["baseline_three_parameter_audit"] = native_audit(
            replica, proof["base_three_parameter_hashes"], host
        )
        record["baseline"] = baseline
        record["live_identity_before"] = before
        guard.save()
        write_pin(guard, 1)
        record["target_health_samples"] = wait_version(live, guard, 1)
        record["target_three_parameter_audit"] = native_audit(
            identity(live, guard)["replicas"][0][2],
            proof["target_three_parameter_hashes"],
            host,
        )
        record["live_target"] = completion(live)
        record["profile_events_target"] = profile_events(replica, host)
        if record["live_target"]["version"] != "1":
            raise RuntimeError("Target response used wrong version")
        write_pin(guard, 0)
        record["restore_health_samples"] = wait_version(live, guard, 0)
        record["restore_three_parameter_audit"] = native_audit(
            identity(live, guard)["replicas"][0][2],
            proof["base_three_parameter_hashes"],
            host,
        )
        record["restored"] = completion(live)
        after = identity(live, guard)
        if before != after or record["restored"] != baseline:
            raise RuntimeError("Restore changed native identity or base output/bytes")
        record["live_identity_after"] = after
        record["profile_events_all"] = profile_events(replica, host)
        record["validated_profile_events"] = validate_profile_events(
            record["profile_events_all"]
        )
        record["live_final_failure_metrics"] = require_zero_failures(live)
        guard.save()
        guard.stop_group(record["name"] + "-serve", live)
        guard.groups.remove((record["name"] + "-serve", live))
        fresh = deploy("-fresh", reference=True)
        fresh_identity = identity(fresh, guard)
        record["fresh_three_parameter_audit"] = native_audit(
            fresh_identity["replicas"][0][2],
            proof["target_three_parameter_hashes"],
            host,
        )
        record["fresh_target"] = completion(fresh)
        record["fresh_failure_metrics"] = require_zero_failures(fresh)
        if (
            record["fresh_target"]["version"] != "0"
            or record["fresh_target"]["output"] != record["live_target"]["output"]
        ):
            raise RuntimeError("Fresh independent target output differs")
        record["status"] = "passed"
        record["native_audit_scope"] = (
            "three independently byte-verified unfused parameters; whole-model CPU reconstruction separately"
        )
    except BaseException as error:
        record.update(status="failed", error_type=type(error).__name__)
        raise
    finally:
        stop_writer(record, out)
        guard.cleanup_compute()
        errors = cleanup_data(guard)
        record["cleanup_errors"] = errors
        guard.save()
        if errors:
            raise RuntimeError(
                "Profile data cleanup incomplete; watchdog remains armed"
            )
        write_json(out / "watchdog-disarmed", {"phase_id": record["gpu_phase_id"]})
        watch.wait(timeout=45)
        if watch.returncode or not (out / "watchdog-exited.json").exists():
            raise RuntimeError("Profile watchdog exit unconfirmed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    execute(args.plan.resolve(), args.out.resolve(), args.authorization, args.env_file)


if __name__ == "__main__":
    main()
