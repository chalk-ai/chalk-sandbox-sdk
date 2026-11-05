"""Execute a separately authorized600-second zeroGPU synthetic pipeline."""

from __future__ import annotations

import argparse
import base64
import importlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from profile_sharded_plan import require_authorization, sha, write_json


def guest_payload():
    return r"""
import base64, hashlib, importlib.util, io, os, sys, tarfile
from pathlib import Path
root=Path(CONFIG['payload_root']); root.mkdir(parents=True,exist_ok=False)
for name in ('home','cache','tmp'): (root/name).mkdir()
os.environ.update(HOME=str(root/'home'),XDG_CACHE_HOME=str(root/'cache'),TMPDIR=str(root/'tmp'))
spec=importlib.util.find_spec('chalkcompute'); assert spec and spec.origin
with tarfile.open(fileobj=io.BytesIO(base64.b64decode(CONFIG['overlay']))) as archive:
    archive.extractall(Path(spec.origin).parent.parent,filter='data')
for name,value in CONFIG['payload_sources'].items():
    data=base64.b64decode(value)
    assert hashlib.sha256(data).hexdigest()==CONFIG['source_hashes'][name]
    (root/name).write_bytes(data)
sys.path.insert(0,str(root))
import chalkcompute._volume as volume_module
original_volume_init=volume_module.Volume.__init__
def existing_only(self,name,*args,**kwargs):
 assert name in (CONFIG['name'],CONFIG['name']+'-reference')
 kwargs['create_if_missing']=False
 original_volume_init(self,name,*args,**kwargs)
volume_module.Volume.__init__=existing_only
from profile_sharded_guest import run
proof=run(CONFIG)
"""


def build_guest_program(out, plan, name, phase, deadline):
    import cpu_seed as guard

    names = (
        "profile_fixture.py",
        "profile_streaming.py",
        "profile_sharded.py",
        "profile_sharded_metrics.py",
        "profile_sharded_guest.py",
    )
    payloads = {
        name: base64.b64encode((out / "guard" / name).read_bytes()).decode()
        for name in names
    }
    config = {
        "name": name,
        "payload_root": "/tmp/weight-sharded-payload-" + phase,
        "payload_sources": payloads,
        "source_hashes": {name: plan["guard_sources"][name] for name in payloads},
        "overlay": base64.b64encode((out / "runtime.tar.gz").read_bytes()).decode(),
        "plan_sha256": sha(out / "plan.json"),
        "work_deadline_epoch": deadline - 120,
    }
    program, poll, root = guard.make_guest_program(phase, guest_payload(), config)
    if len(program.encode()) > 120 * 1024:
        raise ValueError("Synthetic entrypoint exceeds120KiB bound")
    return program, poll, root


def execute(out, authorization_path, env_file):
    began = time.time()
    authorization = json.loads(authorization_path.read_bytes())
    plan = require_authorization(out, authorization, "execute_sharded_synthetic_cpu")
    if sha(Path(__file__)) != plan["guard_sources"]["profile_sharded_remote.py"]:
        raise ValueError("Running CPU launcher differs from frozen source")
    lock = os.open(
        out / "cpu-execution.lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
    )
    os.close(lock)
    sys.path.insert(0, str(out / "guard"))
    guard = importlib.import_module("cpu_seed")
    host = importlib.import_module("profile_host_guard")
    guard.load_env(env_file)
    from profile_cpu_lifecycle import install as install_lifecycle

    install_lifecycle(guard, out / "launcher.json")
    host.install_release_check(guard, out / "launcher.json")
    from profile_volumes import create_volumes, install_cpu_cleanup
    from profile_cpu_lifecycle import (
        guarded_client,
        poll as poll_profile,
        work_remaining,
    )
    import profile_capacity
    from profile_sharded_plan import (
        validate_capacity,
        runtime_capacity_code,
        validate_runtime_limits,
    )

    install_cpu_cleanup(guard, out)
    import chalkcompute as cc
    from chalkcompute._connect import ConnectClient
    from chalkcompute._gen.chalk.server.v1 import team_connect, team_pb

    client = ConnectClient()
    client._ensure_auth()
    environment = client.call(
        client.stub(team_connect.TeamServiceClientSync).get_env, team_pb.GetEnvRequest()
    ).environment
    if environment is None or client._env_id != environment.id:
        raise RuntimeError("CPU authenticated environment differs")
    parts = environment.kube_cluster_name.split("/")
    context = "gke_" + parts[1] + "_" + parts[3] + "_" + parts[5]
    available = subprocess.check_output(
        ["kubectl", "config", "get-contexts", "-o", "name"], text=True, timeout=15
    ).splitlines()
    if context not in available or host.CONTEXT not in available:
        raise RuntimeError("Required cleanup contexts are unavailable")
    contexts = list(
        dict.fromkeys(
            [context] + [c for c in available if "chalk-serverless-stage" in c]
        )
    )
    phase = uuid.uuid4().hex
    name = "weight-profile-" + phase[:10]
    record = {
        "name": name,
        "seed_name": name + "-cpu",
        "cpu_phase_id": phase,
        "launcher_pid": os.getpid(),
        "environment_id": environment.id,
        "expected_namespaces": [environment.id, environment.kube_job_namespace],
        "kube_context": contexts[0],
        "fallback_kube_context": contexts[-1],
        "kube_contexts": contexts,
        "cpu_started_at": began,
        "cpu_deadline_epoch": began + plan["cpu_seconds"],
        "status": "cpu_profile_preparing",
        "compute_class": "HOST",
        "cpu": "4",
        "memory": "8Gi",
        "gpu_claimed": False,
        "owned_volume_names": [name, name + "-reference"],
        "plan_sha256": sha(out / "plan.json"),
        "expected_host_id": host.HOST,
        "source_overlay_sha256": plan["overlay_sha256"],
        "training_deploy_image_uri": plan["image_uri"],
        "prepared_image_sha256": plan["image_json_sha256"],
        "synthetic_fixture": True,
    }
    program, poll, root = build_guest_program(
        out, plan, name, phase, record["cpu_deadline_epoch"]
    )
    (out / "cpu-entrypoint.py").write_text(program)
    record.update(
        guest_root=root, guest_entrypoint_sha256=sha(out / "cpu-entrypoint.py")
    )
    ledger = out / "launcher.json"
    from profile_sharded_guard import arm_watchdog
    from profile_writers import process_identity

    record["launcher_identity"] = process_identity(os.getpid())
    watch = arm_watchdog(guard, record, ledger)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        write_json(out / "host-membership-before-create.json", host.membership())
        capacity = profile_capacity.collect_raw()
        write_json(out / "host-live-capacity-before-create.json", capacity)
        validate_capacity(capacity)
        create_volumes(record, out)
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "CHALK_API_SERVER",
                "CHALK_CLIENT_ID",
                "CHALK_CLIENT_SECRET",
                "CHALK_ACTIVE_ENVIRONMENT",
                "CHALK_ENVIRONMENT",
                "CHALK_ENVIRONMENT_ID",
            }
        }
        env.update(
            PYTHONUNBUFFERED="1",
            OMP_NUM_THREADS="4",
            MKL_NUM_THREADS="4",
            PYTHONDONTWRITEBYTECODE="1",
        )
        # Reserve the complete120s lost-create/reconciliation window in the
        # relative backend TTL, rather than granting600s after a late create.
        ttl = max(1, int(work_remaining(record)))
        record["sandbox_lifetime_seconds"] = ttl
        write_json(ledger, record)
        sandbox = cc.Sandbox(
            name=record["seed_name"],
            image=plan["image_uri"],
            entrypoint=["python3", "-u", "-c", program],
            restart_policy=cc.RestartPolicy.NEVER,
            cpu="4",
            memory="8Gi",
            compute_class=cc.ComputeClass.HOST,
            runtime=cc.Runtime.GVISOR,
            host_pool_id=host.POOL,
            execution_target=cc.ExecutionBackend.SERVERLESS,
            lifetime=str(ttl) + "s",
            chalk_identity=False,
            env=env,
        )
        write_json(out / "host-membership-before-sandbox.json", host.membership())
        capacity = profile_capacity.collect_raw()
        write_json(out / "host-live-capacity-before-sandbox.json", capacity)
        validate_capacity(capacity)
        guarded_client(guard, record, ledger).run(
            sandbox, timeout=min(600, work_remaining(record))
        )
        from training_smoke import ResourceGuard

        placement = ResourceGuard(name, out, record, contexts[0], contexts[-1])
        resources = placement.capture_container_resource(sandbox.id, "seed_resources")
        placements = [
            item.get("status", {}).get("host_placement")
            for resource in resources
            for item in placement.kube_items(
                "chalkcontainers", name=resource["name"], context=resource["context"]
            )
        ]
        if len(placements) == 1 and isinstance(placements[0], dict):
            record["observed_host_id"] = placements[0].get("host_id")
            guard.write_json(ledger, record)
        write_json(out / "host-placement.json", host.require_placement(placements))
        write_json(
            out / "host-runtime-identity.json", host.runtime_identity(sandbox.id)
        )
        runtime = json.loads((out / "host-runtime-identity.json").read_bytes())
        if not isinstance(runtime.get("pid"), int) or runtime["pid"] <= 0:
            raise RuntimeError("Missing exact owned native runtime PID")
        cgroups = json.loads(
            host.ssh(
                [
                    "sudo",
                    "-n",
                    "python3",
                    "-c",
                    runtime_capacity_code(),
                    str(runtime["pid"]),
                ]
            )
        )
        write_json(
            out / "owned-runtime-cgroup.json", {"runtime": runtime, "capacity": cgroups}
        )
        validate_runtime_limits(cgroups)
        progress = poll_profile(sandbox, record, ledger, poll)
        proof = progress["proof"]
        write_json(out / "cpu-proof.json", proof)
        after_runtime = host.runtime_identity(sandbox.id)
        if any(after_runtime.get(key) != runtime.get(key) for key in ("id", "pid")):
            raise RuntimeError("Owned runtime identity changed during measurement")
        after_capacity = json.loads(
            host.ssh(
                [
                    "sudo",
                    "-n",
                    "python3",
                    "-c",
                    runtime_capacity_code(),
                    str(runtime["pid"]),
                ]
            )
        )
        write_json(
            out / "owned-runtime-cgroup-after.json",
            {"runtime": after_runtime, "capacity": after_capacity},
        )
        validate_runtime_limits(after_capacity)
        record.update(
            snapshot_id=proof["snapshot_id"],
            reference_snapshot_id=proof["reference_snapshot_id"],
            proof_sha256=sha(out / "cpu-proof.json"),
        )
        record["status"] = "proof_complete"
        guard.write_json(ledger, record)
    finally:
        guard.release_cpu(record, out)
        if record.get("submission_inflight"):
            raise RuntimeError("Ambiguous CPU submission; detached guard remains armed")
        guard.release_data(record)
        record["status"] = "released"
        guard.write_json(ledger, record)
        guard.write_json(out / "cpu-watchdog-disarmed.json", {"phase_id": phase})
        watch.wait(timeout=15)
        if watch.returncode or not (out / "cpu-watchdog-exited.json").exists():
            raise RuntimeError("CPU cleanup watchdog exit unconfirmed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    execute(args.plan.resolve(), args.authorization, args.env_file)


if __name__ == "__main__":
    main()
