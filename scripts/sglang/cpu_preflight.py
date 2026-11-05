"""Prepare or execute one fixed-lease CPU SGLang preflight. Never claims a GPU.

Preparation is local and requires no credentials. Execution is explicitly opt-in
and reuses a frozen copy of the reviewed weight_sync CPU lifecycle helpers.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path

import preflight_payload as payload

GUARD_FILES = ("cpu_seed.py", "cpu_seed_guest.py", "training_smoke.py", "ftqa_smoke.py")
LEASE_SECONDS = 900


def read_json(path):
    return json.loads(path.read_bytes())


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2))
    temporary.replace(path)


def overlay_archive(source_root):
    package = source_root / "chalkcompute"
    paths = [
        package / name
        for name in ("__init__.py", "_weights_codec.py", "_weights_format.py")
    ]
    paths.extend(sorted((package / "weights" / "serving").rglob("*.py")))
    if not paths or any(not path.is_file() or path.is_symlink() for path in paths):
        raise ValueError("Runtime overlay requires ordinary source files")
    target = io.BytesIO()
    with tarfile.open(fileobj=target, mode="w:gz") as archive:
        for path in paths:
            archive.add(path, arcname=path.relative_to(source_root), recursive=False)
    encoded = target.getvalue()
    if len(encoded) > 64 * 1024:
        raise ValueError("Pure Python serving overlay exceeds64KiB bound")
    return encoded, {
        str(path.relative_to(source_root)): payload.digest(path) for path in paths
    }


def prepare(out, source_root, guard_root, audited_source_root, *, host_prewarm=False):
    out.mkdir(parents=True, exist_ok=False)
    frozen = out / "guard"
    frozen.mkdir()
    guards = {}
    for name in GUARD_FILES:
        source = guard_root / name
        (frozen / name).write_bytes(source.read_bytes())
        guards[name] = payload.digest(frozen / name)
    archive, files = overlay_archive(source_root)
    (out / "overlay.tar.gz").write_bytes(archive)
    (out / "preflight_payload.py").write_bytes(Path(payload.__file__).read_bytes())
    (out / "cpu_preflight.py").write_bytes(Path(__file__).read_bytes())
    if host_prewarm:
        (out / "host_guard.py").write_bytes(
            Path(__file__).with_name("host_guard.py").read_bytes()
        )
    source_hashes = {
        relative: payload.digest(audited_source_root / filename)
        for relative, filename in payload.SOURCE_FILES.items()
    }
    plan = {
        "kind": "sglang_host_prewarm_plan"
        if host_prewarm
        else "sglang_cpu_preflight_plan",
        "image": payload.IMAGE,
        "lease_seconds": LEASE_SECONDS,
        "cpu": "4",
        "memory": "16Gi",
        "compute_class": "HOST" if host_prewarm else "K8S",
        "gpu_claimed": False,
        "chalk_identity": False,
        "owned_volume_names": [],
        "overlay_sha256": payload.digest(out / "overlay.tar.gz"),
        "overlay_files": files,
        "payload_sha256": payload.digest(out / "preflight_payload.py"),
        "launcher_sha256": payload.digest(out / "cpu_preflight.py"),
        "guard_files": guards,
        "source_hashes": source_hashes,
        "host_guard_sha256": payload.digest(out / "host_guard.py")
        if host_prewarm
        else None,
        "host_pool_id": "ExternalClusterHosts" if host_prewarm else None,
        "expected_host_id": "svlsbfe05ebc-oracle-a100" if host_prewarm else None,
    }
    write_json(out / "plan.json", plan)
    return plan


def validate_prepared(out):
    plan = read_json(out / "plan.json")
    host = plan["kind"] == "sglang_host_prewarm_plan"
    if not (
        plan["kind"] in {"sglang_cpu_preflight_plan", "sglang_host_prewarm_plan"}
        and plan["image"] == payload.IMAGE
        and plan["lease_seconds"] == LEASE_SECONDS
        and plan["cpu"] == "4"
        and plan["memory"] == "16Gi"
        and plan["compute_class"] == ("HOST" if host else "K8S")
        and plan["gpu_claimed"] is False
        and plan["chalk_identity"] is False
        and plan["owned_volume_names"] == []
        and plan.get("host_pool_id") == ("ExternalClusterHosts" if host else None)
        and plan.get("expected_host_id")
        == ("svlsbfe05ebc-oracle-a100" if host else None)
    ):
        raise ValueError("CPU preflight resource/image plan changed")
    if host and payload.digest(out / "host_guard.py") != plan["host_guard_sha256"]:
        raise ValueError("Reviewed HOST guard changed")
    if not host and plan.get("host_guard_sha256") is not None:
        raise ValueError("K8S plan cannot select a HOST guard")
    if (
        payload.digest(out / "overlay.tar.gz") != plan["overlay_sha256"]
        or payload.digest(out / "preflight_payload.py") != plan["payload_sha256"]
    ):
        raise ValueError("Prepared CPU source changed")
    if (
        payload.digest(out / "cpu_preflight.py") != plan["launcher_sha256"]
        or payload.digest(Path(__file__)) != plan["launcher_sha256"]
    ):
        raise ValueError("Reviewed CPU launcher source changed")
    if set(plan["guard_files"]) != set(GUARD_FILES) or any(
        payload.digest(out / "guard" / name) != plan["guard_files"][name]
        for name in GUARD_FILES
    ):
        raise ValueError("Reviewed CPU guard source changed")
    return plan


def guest_code():
    return """import base64,io,sys,tarfile
from pathlib import Path
root=Path(CONFIG['root'])
root.mkdir(parents=True,exist_ok=True)
overlay=root/'overlay'
overlay.mkdir()
with tarfile.open(fileobj=io.BytesIO(base64.b64decode(CONFIG['overlay']))) as archive:
    archive.extractall(overlay,filter='data')
sys.path.insert(0,str(overlay))
namespace={'__name__':'sglang_preflight_payload','__file__':str(root/'preflight_payload.py')}
exec(compile(base64.b64decode(CONFIG['payload']),'sglang-preflight-payload','exec'),namespace)
proof=namespace['run_preflight'](root/'output',CONFIG['source_hashes'])
"""


def make_sandbox(cc, name, image, program, *, host_prewarm=False):
    """Construct only; identity must be explicitly disabled despite SDK default."""
    return cc.Sandbox(
        name=name,
        image=image,
        entrypoint=["/opt/sglang/bin/python", "-u", "-c", program],
        restart_policy=cc.RestartPolicy.NEVER,
        cpu="4",
        memory="16Gi",
        compute_class=cc.ComputeClass.HOST if host_prewarm else cc.ComputeClass.K8S,
        runtime=cc.Runtime.GVISOR if host_prewarm else None,
        host_pool_id="ExternalClusterHosts" if host_prewarm else None,
        execution_target=cc.ExecutionBackend.SERVERLESS,
        lifetime="900s",
        chalk_identity=False,
        env={
            "PYTHONUNBUFFERED": "1",
            "OMP_NUM_THREADS": "2",
            "MKL_NUM_THREADS": "2",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
        },
    )


def execute(out, env_file):
    plan = validate_prepared(out)
    # The exclusive ledger prevents accidental repeat execution of one plan.
    descriptor = os.open(
        out / "execution.lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
    )
    os.close(descriptor)
    sys.path.insert(0, str((out / "guard").resolve()))
    guard = importlib.import_module("cpu_seed")
    guard.load_env(env_file)
    host = None
    if plan["kind"] == "sglang_host_prewarm_plan":
        host = importlib.import_module("host_guard")
        host.install_release_check(guard)
        write_json(out / "host-membership-before-auth.json", host.membership())
    import chalkcompute as cc
    from chalkcompute._connect import ConnectClient
    from chalkcompute._gen.chalk.server.v1 import team_connect, team_pb

    client = ConnectClient()
    client._ensure_auth()
    environment = client.call(
        client.stub(team_connect.TeamServiceClientSync).get_env, team_pb.GetEnvRequest()
    ).environment
    if environment.id != client._env_id:
        raise RuntimeError("CPU preflight environment mismatch")
    parts = environment.kube_cluster_name.split("/")
    context = "gke_" + parts[1] + "_" + parts[3] + "_" + parts[5]
    available = subprocess.check_output(
        ["kubectl", "config", "get-contexts", "-o", "name"], text=True, timeout=15
    ).splitlines()
    if context not in available:
        raise RuntimeError("Authenticated environment cleanup context unavailable")
    contexts = list(
        dict.fromkeys(
            [context]
            + [value for value in available if "chalk-serverless-stage" in value]
        )
    )
    phase = uuid.uuid4().hex
    name = "sglang-cpu-preflight-" + phase[:10]
    now = time.time()
    record = {
        "name": name,
        "seed_name": name,
        "cpu_phase_id": phase,
        "launcher_pid": os.getpid(),
        "environment_id": client._env_id,
        "expected_namespaces": [environment.id, environment.kube_job_namespace],
        "kube_context": contexts[0],
        "fallback_kube_context": contexts[-1],
        "kube_contexts": contexts,
        "cpu_started_at": now,
        "cpu_deadline_epoch": now + LEASE_SECONDS,
        "status": "preparing",
        "compute_class": plan["compute_class"],
        "cpu": "4",
        "memory": "16Gi",
        "gpu_claimed": False,
        "owned_volume_names": [],
        "plan_sha256": payload.digest(out / "plan.json"),
        "expected_host_id": plan.get("expected_host_id"),
    }
    root = "/tmp/sglang-preflight-" + phase
    config = {
        "root": root,
        "overlay": base64.b64encode((out / "overlay.tar.gz").read_bytes()).decode(),
        "payload": base64.b64encode(
            (out / "preflight_payload.py").read_bytes()
        ).decode(),
        "source_hashes": plan["source_hashes"],
    }
    program, poll, progress_root = guard.make_guest_program(phase, guest_code(), config)
    if len(program.encode()) > 120 * 1024:
        raise ValueError("CPU entrypoint exceeds safe Linux argument size")
    (out / "guest-entrypoint.py").write_text(program)
    record.update(
        guest_entrypoint_sha256=payload.digest(out / "guest-entrypoint.py"),
        guest_root=progress_root,
    )
    ledger = out / "launcher.json"
    watch = (
        host.arm_watchdog(guard, record, ledger)
        if host
        else guard.arm_cpu_watchdog(record, ledger)
    )

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        sandbox_client = guard.guarded_sandbox_client(record, ledger)
        if host:
            write_json(out / "host-membership-before-create.json", host.membership())
        sandbox = make_sandbox(
            cc, name, plan["image"], program, host_prewarm=host is not None
        )
        sandbox_client.run(sandbox, timeout=min(600, guard.remaining(record) - 120))
        if host:
            from training_smoke import ResourceGuard

            placement_guard = ResourceGuard(
                name, out, record, contexts[0], contexts[-1]
            )
            resources = placement_guard.capture_container_resource(
                sandbox.id, "seed_resources"
            )
            if not resources:
                raise RuntimeError("HOST placement resource identity missing")
            placements = []
            for resource in resources:
                items = placement_guard.kube_items(
                    "chalkcontainers",
                    name=resource["name"],
                    context=resource["context"],
                )
                placements.extend(
                    item.get("status", {}).get("host_placement") for item in items
                )
            if len(placements) == 1 and isinstance(placements[0], dict):
                record["observed_host_id"] = placements[0].get("host_id")
                write_json(ledger, record)
            write_json(out / "host-placement.json", host.require_placement(placements))
            identity = host.runtime_identity(sandbox.id)
            record["host_runtime_id"] = identity["id"]
            write_json(out / "host-runtime-identity.json", identity)
            write_json(ledger, record)
        progress = guard.poll_guest_until_complete(sandbox, record, ledger, poll)
        proof = progress["proof"]
        write_json(out / "cpu-proof.json", proof)
        bundle = guard.read_guest_file(
            sandbox,
            root + "/output/fixture.tar.gz",
            max_bytes=payload.MAX_BUNDLE_BYTES,
            record=record,
        )
        if (
            len(bundle) != proof["bundle"]["bytes"]
            or hashlib.sha256(bundle).hexdigest() != proof["bundle"]["sha256"]
        ):
            raise RuntimeError("CPU fixture export differs from immutable proof")
        (out / "fixture.tar.gz").write_bytes(bundle)
        record.update(
            status="cpu_preflight_passed",
            preflight_status="passed",
            proof_sha256=payload.digest(out / "cpu-proof.json"),
            fixture_sha256=payload.digest(out / "fixture.tar.gz"),
        )
        write_json(ledger, record)
    except BaseException as error:
        record.update(preflight_status="failed", error_type=type(error).__name__)
        write_json(ledger, record)
        raise
    finally:
        # Guard remains armed if cleanup/ambiguous-create reconciliation fails.
        guard.release_cpu(record, out)
        if record.get("submission_inflight"):
            raise RuntimeError(
                "Ambiguous CPU create; detached reconciliation remains armed"
            )
        guard.release_data(record)
        record["status"] = "released"
        write_json(ledger, record)
        write_json(out / "cpu-watchdog-disarmed.json", {"phase_id": phase})
        watch.wait(timeout=15)
        if watch.returncode or not (out / "cpu-watchdog-exited.json").exists():
            raise RuntimeError("CPU preflight guard exit unconfirmed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--guard-root", type=Path)
    parser.add_argument("--audited-source-root", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--host-prewarm",
        action="store_true",
        help="Prepare fixed Oracle CPU HOST plan; claims nothing",
    )
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    if args.execute:
        if not args.env_file:
            parser.error("--execute requires --env-file")
        execute(args.out.resolve(), args.env_file)
    else:
        if not all((args.source_root, args.guard_root, args.audited_source_root)):
            parser.error(
                "Preparation requires --source-root, --guard-root, --audited-source-root"
            )
        prepare(
            args.out.resolve(),
            args.source_root.resolve(),
            args.guard_root.resolve(),
            args.audited_source_root.resolve(),
            host_prewarm=args.host_prewarm,
        )


if __name__ == "__main__":
    main()
