"""Execute only an independently authorized frozen CPU profile plan; never a GPU."""

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

from profile_plan import require_authorization, sha, write_json


def guest_payload():
    return r"""
import base64, hashlib, importlib.util, io, sys, tarfile
from pathlib import Path
root=Path(CONFIG['payload_root']); root.mkdir(parents=True,exist_ok=False)
spec=importlib.util.find_spec('chalkcompute')
assert spec and spec.origin
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
from profile_fixture import check_workspace
workspace_proof=check_workspace(root,80*1024**3)
(root/'workspace-proof.json').write_text(__import__('json').dumps(workspace_proof))
namespace={'CONFIG':(CONFIG['name'],CONFIG['model'],CONFIG['revision'])}
exec(compile(CONFIG['seed_code'],'independent-hf-seed-proof','exec'),namespace)
seed_proof=namespace['proof']
from chalkcompute.weights import WeightStream
from profile_cpu_payload import prepare_fixture
proof=prepare_fixture(WeightStream(CONFIG['name'],CONFIG['model']),namespace['folder'],root/'fixture')
proof.update(workspace=workspace_proof,model=CONFIG['model'],revision=CONFIG['revision'],seed_proof=seed_proof,plan_sha256=CONFIG['plan_sha256'])
"""


def build_guest_program(out, plan, name, phase):
    import cpu_seed as guard

    payloads = {
        name: base64.b64encode((out / "guard" / name).read_bytes()).decode()
        for name in ("profile_fixture.py", "profile_cpu_payload.py")
    }
    config = {
        "name": name,
        "model": plan["model"],
        "revision": plan["revision"],
        "payload_root": "/tmp/weight-profile-payload-" + phase,
        "payload_sources": payloads,
        "source_hashes": {name: plan["guard_sources"][name] for name in payloads},
        "overlay": base64.b64encode((out / "runtime.tar.gz").read_bytes()).decode(),
        "seed_code": guard.SEED_CODE,
        "plan_sha256": sha(out / "plan.json"),
    }
    program, poll, root = guard.make_guest_program(phase, guest_payload(), config)
    if len(program.encode()) > 120 * 1024:
        raise ValueError("CPU entrypoint exceeds safe120KiB argument bound")
    return program, poll, root


def execute(out, authorization_path, env_file):
    began = time.time()
    authorization = json.loads(authorization_path.read_bytes())
    plan = require_authorization(out, authorization, "execute_profile_cpu")
    if sha(Path(__file__)) != plan["guard_sources"]["profile_cpu.py"]:
        raise ValueError("Running CPU launcher differs from frozen source")
    lock = os.open(
        out / "cpu-execution.lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
    )
    os.close(lock)
    sys.path.insert(0, str(out / "guard"))
    guard = importlib.import_module("cpu_seed")
    host = importlib.import_module("profile_host_guard")
    guard.load_env(env_file)
    host.install_release_check(guard)
    from profile_volumes import create_volumes, install_cpu_cleanup

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
        "cpu": "16",
        "memory": "128Gi",
        "gpu_claimed": False,
        "owned_volume_names": [name, name + "-reference"],
        "plan_sha256": sha(out / "plan.json"),
        "expected_host_id": host.HOST,
        "source_overlay_sha256": plan["overlay_sha256"],
        "training_deploy_image_uri": plan["image_uri"],
        "prepared_image_sha256": plan["image_json_sha256"],
        "hf_model": plan["model"],
        "hf_revision": plan["revision"],
    }
    program, poll, root = build_guest_program(out, plan, name, phase)
    (out / "cpu-entrypoint.py").write_text(program)
    record.update(
        guest_root=root, guest_entrypoint_sha256=sha(out / "cpu-entrypoint.py")
    )
    ledger = out / "launcher.json"
    watch = host.arm_watchdog(guard, record, ledger)
    transferred = False

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        write_json(out / "host-membership-before-create.json", host.membership())
        disk = json.loads(
            host.ssh(
                [
                    "python3",
                    "-c",
                    "import json,os; s=os.statvfs('/var/lib/docker'); print(json.dumps({'path':'/var/lib/docker','free_bytes':s.f_bavail*s.f_frsize}))",
                ]
            )
        )
        if disk["free_bytes"] < plan["minimum_host_free_disk_bytes"]:
            raise RuntimeError("Insufficient Oracle disk before CPU claim")
        write_json(out / "host-disk-before-create.json", disk)
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
            OMP_NUM_THREADS="16",
            MKL_NUM_THREADS="16",
            HF_HOME=plan["hf_cache"],
            HF_HUB_DISABLE_TELEMETRY="1",
        )
        sandbox = cc.Sandbox(
            name=record["seed_name"],
            image=plan["image_uri"],
            entrypoint=["python3", "-u", "-c", program],
            restart_policy=cc.RestartPolicy.NEVER,
            cpu="16",
            memory="128Gi",
            compute_class=cc.ComputeClass.HOST,
            runtime=cc.Runtime.GVISOR,
            host_pool_id=host.POOL,
            execution_target=cc.ExecutionBackend.SERVERLESS,
            lifetime="1800s",
            chalk_identity=False,
            env=env,
        )
        guard.guarded_sandbox_client(record, ledger).run(
            sandbox, timeout=min(600, guard.remaining(record) - 120)
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
        progress = guard.poll_guest_until_complete(sandbox, record, ledger, poll)
        proof = progress["proof"]
        write_json(out / "cpu-proof.json", proof)
        record.update(
            snapshot_id=proof["snapshot_id"],
            reference_snapshot_id=proof["reference_snapshot_id"],
            proof_sha256=sha(out / "cpu-proof.json"),
        )
        guard.release_cpu(record, out)
        record["status"] = "paused_proof"
        guard.write_json(ledger, record)
        while guard.remaining(record) > 0:
            auth_path = out / "cpu-authorization.json"
            if auth_path.exists():
                transfer = json.loads(auth_path.read_bytes())
                if (
                    not guard.valid_handoff(record, transfer)
                    or transfer.get("plan_sha256") != record["plan_sha256"]
                ):
                    raise RuntimeError(
                        "CPU transfer authorization differs from exact frozen plan/proof"
                    )
                guard.write_json(
                    out / "cpu-watchdog-disarmed.json", {"phase_id": phase}
                )
                watch.wait(timeout=15)
                if watch.returncode or not (out / "cpu-watchdog-exited.json").exists():
                    raise RuntimeError("CPU transfer watchdog exit unconfirmed")
                transferred = True
                record.update(
                    status="data_ownership_transferred",
                    gpu_phase_id=transfer["gpu_phase_id"],
                )
                guard.write_json(ledger, record)
                return
            time.sleep(1)
    finally:
        if not transferred:
            guard.release_cpu(record, out)
            if record.get("submission_inflight"):
                raise RuntimeError(
                    "Ambiguous CPU submission; detached guard remains armed"
                )
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
