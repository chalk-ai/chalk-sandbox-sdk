"""Prepare or execute exactly one proof-bound A100 SGLang smoke, hard900s.

Preparation is local. Execution requires reviewed immutable CPU HOST evidence,
starts an independent guard before volume metadata/upload, and owns all names.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

import cpu_preflight as base
import gpu_guard
import preflight_payload as payload

LEASE_SECONDS = 900
LOCAL_FILES = (
    "cpu_preflight.py",
    "preflight_payload.py",
    "host_guard.py",
    "gpu_guard.py",
    "gpu_payload.py",
    "gpu_smoke.py",
)


def prepare(out, source_root, guard_root, audited_source_root, cpu_proof_root):
    previous = base.read_json(cpu_proof_root / "launcher.json")
    if (
        previous.get("status") != "released"
        or previous.get("preflight_status") != "passed"
        or previous.get("host_runtime_verified_absent") is not True
    ):
        raise ValueError("Require passed and released CPU HOST proof")
    proof = base.read_json(cpu_proof_root / "cpu-proof.json")
    cache = base.read_json(cpu_proof_root / "independent-host-cache-proof.json")
    cleanup = base.read_json(cpu_proof_root / "independent-cleanup-proof.json")
    cpu_plan = base.read_json(cpu_proof_root / "plan.json")
    if (
        previous.get("proof_sha256")
        != payload.digest(cpu_proof_root / "cpu-proof.json")
        or previous.get("plan_sha256") != payload.digest(cpu_proof_root / "plan.json")
        or previous.get("fixture_sha256")
        != payload.digest(cpu_proof_root / "fixture.tar.gz")
        or proof.get("image") != payload.IMAGE
    ):
        raise ValueError("CPU ledger does not bind exact proof/plan/fixture")
    if cache["image"] != payload.IMAGE or not any(
        item.get("complete_marker") and item.get("mounted_rootfs")
        for item in cache["hypervisor_exact_digest_cache_entries"]
    ):
        raise ValueError("Exact complete mounted HOST image cache required")
    if (
        not cleanup["native_host"]["runsc_absent"]
        or not cleanup["native_host"]["containerd_task_absent"]
        or any(item["owned_count"] for item in cleanup["cr_and_owner_uid_pod_checks"])
    ):
        raise ValueError("Independent CPU cleanup proof is incomplete")
    fixture = (cpu_proof_root / "fixture.tar.gz").read_bytes()
    if (
        len(fixture) != proof["bundle"]["bytes"]
        or hashlib.sha256(fixture).hexdigest() != proof["bundle"]["sha256"]
    ):
        raise ValueError("HOST fixture differs from exact new guest proof")
    plan = base.prepare(
        out, source_root, guard_root, audited_source_root, host_prewarm=True
    )
    if (
        plan["overlay_files"] != cpu_plan["overlay_files"]
        or plan["source_hashes"] != proof["source_sha256"]
    ):
        raise ValueError(
            "GPU runtime/native source differs from successful CPU HOST proof"
        )
    (out / "cpu-plan.json").write_bytes((cpu_proof_root / "plan.json").read_bytes())
    (out / "cpu-launcher.json").write_bytes(
        (cpu_proof_root / "launcher.json").read_bytes()
    )
    for name in LOCAL_FILES:
        (out / name).write_bytes(Path(__file__).with_name(name).read_bytes())
    (out / "fixture.tar.gz").write_bytes(fixture)
    for name in (
        "cpu-proof.json",
        "independent-cleanup-proof.json",
        "independent-host-cache-proof.json",
    ):
        (out / name).write_bytes((cpu_proof_root / name).read_bytes())
    plan.update(
        kind="sglang_gpu_smoke_plan",
        cpu="8",
        memory="32Gi",
        gpu="nvidia-a100-80gb:1",
        gpu_claimed=True,
        cleanup_reserve_seconds=120,
        cold_start_seconds=300,
        restart_seconds=180,
        update_seconds=30,
        no_progress_seconds=180,
        tolerance=1e-3,
        minimum_change=0.01,
        scope="native supervisor correctness; immutable fixture copied locally; guest-local CURRENT/PIN",
        owned_volume_contract="unique phase name;8MiB bound;immutable version mount",
        fixture_sha256=payload.digest(out / "fixture.tar.gz"),
        fixture_bytes=len(fixture),
        evidence_files={
            name: payload.digest(out / name)
            for name in (
                "cpu-proof.json",
                "cpu-plan.json",
                "cpu-launcher.json",
                "independent-cleanup-proof.json",
                "independent-host-cache-proof.json",
            )
        },
        local_files={name: payload.digest(out / name) for name in LOCAL_FILES},
    )
    base.write_json(out / "plan.json", plan)
    return plan


def validate(out):
    plan = base.read_json(out / "plan.json")
    exact = dict(
        kind="sglang_gpu_smoke_plan",
        image=payload.IMAGE,
        lease_seconds=900,
        cpu="8",
        memory="32Gi",
        gpu="nvidia-a100-80gb:1",
        compute_class="HOST",
        gpu_claimed=True,
        chalk_identity=False,
        cleanup_reserve_seconds=120,
        cold_start_seconds=300,
        restart_seconds=180,
        update_seconds=30,
        no_progress_seconds=180,
        tolerance=1e-3,
        minimum_change=0.01,
        scope="native supervisor correctness; immutable fixture copied locally; guest-local CURRENT/PIN",
        host_pool_id="ExternalClusterHosts",
        expected_host_id="svlsbfe05ebc-oracle-a100",
    )
    if any(plan.get(key) != value for key, value in exact.items()):
        raise ValueError("Fixed GPU smoke contract changed")
    if (
        plan["fixture_bytes"] > 8 * 1024 * 1024
        or (out / "fixture.tar.gz").stat().st_size != plan["fixture_bytes"]
    ):
        raise ValueError("Fixture exceeds fixed8MiB bound")
    if payload.digest(out / "fixture.tar.gz") != plan["fixture_sha256"]:
        raise ValueError("Fixture hash changed")
    if set(plan["local_files"]) != set(LOCAL_FILES) or any(
        payload.digest(out / name) != expected
        for name, expected in plan["local_files"].items()
    ):
        raise ValueError("Frozen local controller/payload/guard changed")
    if payload.digest(Path(__file__)) != plan["local_files"]["gpu_smoke.py"]:
        raise ValueError("Execute the exact frozen GPU controller")
    if set(plan["evidence_files"]) != {
        "cpu-proof.json",
        "cpu-plan.json",
        "cpu-launcher.json",
        "independent-cleanup-proof.json",
        "independent-host-cache-proof.json",
    } or any(
        payload.digest(out / name) != expected
        for name, expected in plan["evidence_files"].items()
    ):
        raise ValueError("Frozen CPU proof/cache/cleanup changed")
    if set(plan["guard_files"]) != set(base.GUARD_FILES) or any(
        payload.digest(out / "guard" / name) != expected
        for name, expected in plan["guard_files"].items()
    ):
        raise ValueError("Reviewed lifecycle source changed")
    if payload.digest(out / "overlay.tar.gz") != plan["overlay_sha256"]:
        raise ValueError("Frozen runtime overlay changed")
    if (
        plan["source_hashes"] != base.read_json(out / "cpu-proof.json")["source_sha256"]
        or plan["overlay_files"]
        != base.read_json(out / "cpu-plan.json")["overlay_files"]
    ):
        raise ValueError("GPU runtime/source hashes differ from frozen CPU proof")
    return plan


def require_guard(record, out):
    if time.time() >= record["cpu_deadline_epoch"] - 120:
        raise TimeoutError("GPU phase reached cleanup reserve")
    ready = base.read_json(out / "cpu-watchdog-ready.json")
    if (
        ready["phase_id"] != record["cpu_phase_id"]
        or ready["pid"] != record["cpu_watchdog_pid"]
    ):
        raise RuntimeError("GPU ownership guard phase/PID mismatch")
    os.kill(ready["pid"], 0)
    if (out / "cpu-expired").exists():
        raise TimeoutError("GPU ownership guard expired")


def create_fixture_volume(record, out, create_call):
    require_guard(record, out)
    now = time.time()
    marker = {
        "phase_id": record["cpu_phase_id"],
        "name": record["owned_volume_names"][0],
        "status": "inflight",
        "started_at": now,
        "rpc_deadline_epoch": now + 30,
        "reconcile_until_epoch": now + 90,
    }
    base.write_json(out / "upload-create.json", marker)
    result = create_call(30)
    if result.volume is None:
        raise RuntimeError("Volume create returned no acknowledgement")
    marker.update(status="acknowledged", acknowledged_at=time.time())
    base.write_json(out / "upload-create.json", marker)
    return result


def upload(out):
    # Child is born inert. Parent binds its exact PID/group/start identity and
    # phase before authorizing any volume metadata access or upload.
    record = base.read_json(out / "launcher.json")
    phase = record["cpu_phase_id"]
    base.write_json(out / "upload-ready.json", {"pid": os.getpid(), "phase_id": phase})
    until = time.time() + 30
    while not (out / "upload-authorized.json").exists():
        if time.time() > until:
            raise TimeoutError("Upload writer was never authorized")
        time.sleep(0.1)
    authorization = base.read_json(out / "upload-authorized.json")
    if authorization != {"pid": os.getpid(), "phase_id": phase}:
        raise RuntimeError("Upload authorization differs")
    require_guard(record, out)
    import chalkcompute as cc
    from chalkcompute._connect import (
        ConnectClient,
        ProtocolType,
        SYNC_HTTP_CLIENT,
        RPC_READ_MAX_BYTES,
    )
    from chalkcompute._gen.chalk.volume.v2 import volume_connect, volume_pb
    from chalkcompute._volume import VolumeNotFoundError

    client = ConnectClient()
    client._ensure_auth()
    if client._env_id != record["environment_id"]:
        raise RuntimeError("Upload environment changed")
    # Metadata creation has no retry interceptor: reconcile its unique name on
    # ambiguity. Native immutable file chunk retries remain bounded by writer life.
    client._typed_clients[volume_connect.VolumeServiceClientSync] = (
        volume_connect.VolumeServiceClientSync(
            client._api_server,
            protocol=ProtocolType.GRPC,
            interceptors=(),
            http_client=SYNC_HTTP_CLIENT,
            read_max_bytes=RPC_READ_MAX_BYTES,
        )
    )
    volume_client = cc.VolumeClient.from_connect(client)
    name = record["owned_volume_names"][0]
    try:
        cc.Volume(name, client=volume_client, create_if_missing=False).reload()
    except VolumeNotFoundError:
        pass
    else:
        raise RuntimeError("Unique owned fixture volume already exists")
    require_guard(record, out)
    create_fixture_volume(
        record,
        out,
        lambda timeout: client.call(
            client._typed_clients[volume_connect.VolumeServiceClientSync].create_volume,
            volume_pb.CreateVolumeRequest(name=name),
            timeout=timeout,
        ),
    )
    volume = cc.Volume(name, client=volume_client, create_if_missing=False)
    volume.reload()
    require_guard(record, out)
    volume.put_file("fixture.tar.gz", (out / "fixture.tar.gz").read_bytes())
    base.write_json(
        out / "upload-result.json",
        {
            "name": name,
            "version_id": volume.version.id,
            "fixture_sha256": payload.digest(out / "fixture.tar.gz"),
            "phase_id": phase,
        },
    )


def upload_fixture(record, out):
    require_guard(record, out)
    with (out / "upload.log").open("wb") as log:
        writer = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--upload",
                "--out",
                str(out),
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            until = time.time() + 15
            while not (out / "upload-ready.json").exists():
                if writer.poll() is not None or time.time() > until:
                    raise RuntimeError("Upload writer not ready")
                time.sleep(0.1)
            ready = base.read_json(out / "upload-ready.json")
            if ready != {"pid": writer.pid, "phase_id": record["cpu_phase_id"]}:
                raise RuntimeError("Upload writer ready identity mismatch")
            record["upload_writer"] = gpu_guard.process_identity(writer.pid)
            base.write_json(out / "launcher.json", record)
            require_guard(record, out)
            base.write_json(out / "upload-authorized.json", ready)
            writer.wait(
                timeout=min(90, record["cpu_deadline_epoch"] - time.time() - 120)
            )
            if writer.returncode:
                raise RuntimeError("Fixture upload writer failed; see upload.log")
        finally:
            gpu_guard.stop_upload(record, out)
            writer.wait(timeout=10)
    result = base.read_json(out / "upload-result.json")
    if (
        result["phase_id"] != record["cpu_phase_id"]
        or result["name"] != record["owned_volume_names"][0]
        or result["fixture_sha256"] != payload.digest(out / "fixture.tar.gz")
    ):
        raise RuntimeError("Immutable fixture upload proof differs")
    return result


def guest_code():
    return """import asyncio,base64,hashlib,io,json,os,sys,tarfile
from pathlib import Path
root=Path(CONFIG['root']); root.mkdir()
overlay=root/'overlay'; overlay.mkdir()
with tarfile.open(fileobj=io.BytesIO(base64.b64decode(CONFIG['overlay']))) as archive: archive.extractall(overlay,filter='data')
sys.path.insert(0,str(overlay));os.environ['PYTHONPATH']=str(overlay)
(root/'preflight_payload.py').write_bytes(base64.b64decode(CONFIG['cpu_payload']))
sys.path.insert(0,str(root))
import preflight_payload as audited
from importlib.metadata import version
import torch
if torch.cuda.device_count()!=1 or 'A100' not in torch.cuda.get_device_name(0): raise RuntimeError('Expected exactly one A100')
audited.validate_versions({name:version(name) for name in ('sglang','torch','transformers')},cuda=torch.version.cuda,hip=torch.version.hip)
import importlib.util
package=Path(next(iter(importlib.util.find_spec('sglang').submodule_search_locations)))
audited.verify_sources(package,CONFIG['source_hashes']);audited.verify_native_schemas(package)
bundle=Path('/fixture/fixture.tar.gz').read_bytes()
if len(bundle)!=CONFIG['fixture_bytes'] or hashlib.sha256(bundle).hexdigest()!=CONFIG['fixture_sha256']: raise RuntimeError('Mounted fixture differs')
fixture=root/'fixture'; fixture.mkdir()
with tarfile.open(fileobj=io.BytesIO(bundle)) as archive: archive.extractall(fixture,filter='data')
script=root/'gpu_payload.py'; script.write_bytes(base64.b64decode(CONFIG['gpu_payload']))
import importlib.util
spec=importlib.util.spec_from_file_location('native_gpu_payload',script); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
from argparse import Namespace
args=Namespace(output=root/'output',fixture=fixture,deadline=CONFIG['deadline'])
try: asyncio.run(module.run(args))
finally:
 if (root/'output'/'gpu-proof.json').exists(): print((root/'output'/'gpu-proof.json').read_text(),flush=True)
proof=json.loads((root/'output'/'gpu-proof.json').read_bytes())
"""


def progress_poll(out, progress_root, guest_root):
    code = (out / "guard" / "cpu_seed_guest.py").read_text()
    code += "\nprogress=read_progress(Path(" + repr(progress_root) + "))\n"
    code += "output=Path(" + repr(guest_root + "/output") + ")\n"
    code += """if (output/'gpu-proof.json').exists(): progress['native_proof']=json.loads((output/'gpu-proof.json').read_bytes())
logs=sorted(output.glob('native-*/native.log'))
if logs:
 progress['native_log_bytes']=logs[-1].stat().st_size
 progress['native_log_name']=logs[-1].parent.name
 progress['native_log_tail']=tail(logs[-1],8192)
print(json.dumps(progress))
"""
    return code


def make_sandbox(cc, record, plan, program, upload_result):
    return cc.Sandbox(
        name=record["seed_name"],
        image=plan["image"],
        entrypoint=["/opt/sglang/bin/python", "-u", "-c", program],
        restart_policy=cc.RestartPolicy.NEVER,
        cpu="8",
        memory="32Gi",
        gpu="nvidia-a100-80gb:1",
        compute_class=cc.ComputeClass.HOST,
        runtime=cc.Runtime.GVISOR,
        host_pool_id="ExternalClusterHosts",
        execution_target=cc.ExecutionBackend.SERVERLESS,
        lifetime=str(max(1, int(record["cpu_deadline_epoch"] - time.time()))) + "s",
        chalk_identity=False,
        volumes=[
            cc.VolumeMount(
                name=upload_result["name"],
                mount_path="/fixture",
                version_id=upload_result["version_id"],
            )
        ],
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
    plan = validate(out)
    descriptor = os.open(
        out / "execution.lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
    )
    os.close(descriptor)
    sys.path.insert(0, str(out / "guard"))
    guard = importlib.import_module("cpu_seed")
    guard.load_env(env_file)
    import host_guard

    gpu_guard.install(guard, out)
    base.write_json(out / "host-membership-before-auth.json", host_guard.membership())
    import chalkcompute as cc
    from chalkcompute._connect import ConnectClient
    from chalkcompute._gen.chalk.server.v1 import team_connect, team_pb

    client = ConnectClient()
    client._ensure_auth()
    environment = client.call(
        client.stub(team_connect.TeamServiceClientSync).get_env, team_pb.GetEnvRequest()
    ).environment
    if environment.id != client._env_id:
        raise RuntimeError("Environment mismatch")
    parts = environment.kube_cluster_name.split("/")
    context = "gke_" + parts[1] + "_" + parts[3] + "_" + parts[5]
    available = subprocess.check_output(
        ["kubectl", "config", "get-contexts", "-o", "name"], text=True, timeout=15
    ).splitlines()
    if context not in available:
        raise RuntimeError("Cleanup context missing")
    contexts = list(
        dict.fromkeys(
            [context]
            + [value for value in available if "chalk-serverless-stage" in value]
        )
    )
    phase = uuid.uuid4().hex
    name = "sglang-gpu-smoke-" + phase[:10]
    now = time.time()
    record = dict(
        name=name,
        seed_name=name,
        cpu_phase_id=phase,
        launcher_pid=os.getpid(),
        environment_id=client._env_id,
        expected_namespaces=[environment.id, environment.kube_job_namespace],
        kube_context=contexts[0],
        fallback_kube_context=contexts[-1],
        kube_contexts=contexts,
        cpu_started_at=now,
        cpu_deadline_epoch=now + LEASE_SECONDS,
        status="preparing",
        compute_class="HOST",
        cpu="8",
        memory="32Gi",
        gpu="nvidia-a100-80gb:1",
        gpu_claimed=True,
        owned_volume_names=[name + "-fixture"],
        plan_sha256=payload.digest(out / "plan.json"),
        expected_host_id=plan["expected_host_id"],
    )
    ledger = out / "launcher.json"
    watch = gpu_guard.arm(guard, record, ledger)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        immutable = upload_fixture(record, out)
        record["snapshot_id"] = immutable["version_id"]
        base.write_json(ledger, record)
        root = "/tmp/sglang-gpu-" + phase
        config = dict(
            root=root,
            overlay=base64.b64encode((out / "overlay.tar.gz").read_bytes()).decode(),
            gpu_payload=base64.b64encode(
                (out / "gpu_payload.py").read_bytes()
            ).decode(),
            cpu_payload=base64.b64encode(
                (out / "preflight_payload.py").read_bytes()
            ).decode(),
            source_hashes=plan["source_hashes"],
            fixture_bytes=plan["fixture_bytes"],
            fixture_sha256=plan["fixture_sha256"],
            deadline=record["cpu_deadline_epoch"],
        )
        # Include CPU audit module in the runtime overlay without importing GPU kernels locally.
        program, _, progress_root = guard.make_guest_program(
            phase, guest_code(), config
        )
        if len(program.encode()) > 120 * 1024:
            raise ValueError("GPU entrypoint exceeds safe argument size")
        (out / "guest-entrypoint.py").write_text(program)
        record["guest_root"] = progress_root
        base.write_json(ledger, record)
        require_guard(record, out)
        base.write_json(
            out / "host-membership-before-create.json", host_guard.membership()
        )
        sandbox = make_sandbox(cc, record, plan, program, immutable)
        guard.guarded_sandbox_client(record, ledger).run(
            sandbox, timeout=min(180, guard.remaining(record) - 120)
        )
        from training_smoke import ResourceGuard

        placement = ResourceGuard(name, out, record, contexts[0], contexts[-1])
        resources = placement.capture_container_resource(sandbox.id, "seed_resources")
        locations = []
        for resource in resources:
            locations.extend(
                item.get("status", {}).get("host_placement")
                for item in placement.kube_items(
                    "chalkcontainers",
                    name=resource["name"],
                    context=resource["context"],
                )
            )
        if len(locations) == 1 and isinstance(locations[0], dict):
            record["observed_host_id"] = locations[0].get("host_id")
            base.write_json(ledger, record)
        base.write_json(
            out / "host-placement.json", host_guard.require_placement(locations)
        )
        native = host_guard.runtime_identity(sandbox.id)
        record["host_runtime_id"] = native["id"]
        base.write_json(out / "host-runtime-identity.json", native)
        base.write_json(ledger, record)
        poll = progress_poll(out, progress_root, root)
        last_progress_at, previous_fingerprint = time.time(), None
        # Only short read-only progress execs are replayable; the payload belongs to PID1.
        while time.time() < record["cpu_deadline_epoch"] - 120:
            response = sandbox.exec(
                "/opt/sglang/bin/python", "-c", poll, timeout_secs=20
            )
            if response.error is not None or response.exit_code not in (0, None):
                raise RuntimeError("Read-only GPU progress poll failed")
            progress = json.loads(response.stdout_text)
            base.write_json(out / "guest-progress.json", progress)
            native_proof = progress.get("native_proof")
            if native_proof:
                base.write_json(out / "gpu-proof.json", native_proof)
            fingerprint = (
                progress.get("native_log_name"),
                progress.get("native_log_bytes"),
                json.dumps(native_proof, sort_keys=True),
            )
            if fingerprint != previous_fingerprint:
                last_progress_at, previous_fingerprint = time.time(), fingerprint
            if time.time() - last_progress_at > 180:
                raise TimeoutError(
                    "GPU scenario made no native/test progress for180seconds"
                )
            if progress.get("completed"):
                if progress["completed"]["phase_id"] != phase:
                    raise RuntimeError("Guest phase mismatch")
                if progress["completed"]["status"] != "passed":
                    raise RuntimeError(
                        "Native smoke failed; see retained guest progress"
                    )
                proof = progress["proof"]
                base.write_json(out / "gpu-proof.json", proof)
                if (
                    proof["status"] != "passed"
                    or proof.get("native_groups_stopped") is not True
                ):
                    raise RuntimeError("Native proof incomplete")
                record.update(status="gpu_smoke_passed", smoke_status="passed")
                base.write_json(ledger, record)
                break
            time.sleep(2)
        else:
            raise TimeoutError("GPU smoke reached cleanup reserve")
    except BaseException as error:
        record.update(smoke_status="failed", error_type=type(error).__name__)
        base.write_json(ledger, record)
        raise
    finally:
        guard.release_cpu(record, out)
        if record.get("submission_inflight"):
            raise RuntimeError("Ambiguous GPU create remains guarded")
        guard.release_data(record)
        record["status"] = "released"
        base.write_json(ledger, record)
        base.write_json(out / "cpu-watchdog-disarmed.json", {"phase_id": phase})
        watch.wait(timeout=15)
        if watch.returncode or not (out / "cpu-watchdog-exited.json").exists():
            raise RuntimeError("GPU watchdog exit unconfirmed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--env-file", type=Path)
    for option in (
        "source-root",
        "guard-root",
        "audited-source-root",
        "cpu-proof-root",
    ):
        parser.add_argument("--" + option, type=Path)
    args = parser.parse_args()
    if args.upload:
        upload(args.out.resolve())
    elif args.execute:
        if not args.env_file:
            parser.error("--execute requires --env-file")
        execute(args.out.resolve(), args.env_file)
    else:
        if not all(
            (
                args.source_root,
                args.guard_root,
                args.audited_source_root,
                args.cpu_proof_root,
            )
        ):
            parser.error("Preparation requires four source/proof roots")
        prepare(
            args.out.resolve(),
            args.source_root,
            args.guard_root,
            args.audited_source_root,
            args.cpu_proof_root,
        )
