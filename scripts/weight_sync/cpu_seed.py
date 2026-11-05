"""CPU-only bounded HF seed and immutable proof; never claims GPU compute.

The detached watchdog starts before create. A successful seed stops its CPU pod
and pauses under the original data lease until an explicit, proof-bound ownership
transfer. Expiry or launcher death releases compute first and then its volume.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from ftqa_smoke import MODELS, load_env
from training_smoke import ResourceGuard

LEASE_SECONDS = 1200
STARTUP_SECONDS = 900
CREATE_RPC_SECONDS = 60
CREATE_RECONCILE_GRACE_SECONDS = 60


def write_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2))
    temporary.replace(path)


def remaining(record):
    seconds = record["cpu_deadline_epoch"] - time.time()
    if seconds <= 0:
        raise TimeoutError("CPU lease expired")
    return seconds


def valid_handoff(record, authorization):
    """Accept only this proof, phase, volume, and a future ready GPU guard."""
    return (
        record.get("status") == "paused_proof"
        and authorization.get("cpu_phase_id") == record["cpu_phase_id"]
        and authorization.get("proof_sha256") == record.get("proof_sha256")
        and authorization.get("volume_name") == record["name"]
        and authorization.get("snapshot_id") == record.get("snapshot_id")
        and authorization.get("hf_revision") == record.get("hf_revision")
        and authorization.get("prepared_image_sha256")
        == record.get("prepared_image_sha256")
        and authorization.get("source_overlay_sha256")
        == record.get("source_overlay_sha256")
        and authorization.get("training_deploy_image_uri")
        == record.get("training_deploy_image_uri")
        and authorization.get("owns_transferred_source_volume") is True
        and record["cpu_deadline_epoch"] > time.time()
        and authorization.get("action") == "transfer_data_ownership"
        and isinstance(authorization.get("gpu_phase_id"), str)
        and bool(authorization["gpu_phase_id"])
        and authorization.get("gpu_guard_ready") is True
        and authorization.get("gpu_deadline_epoch", 0) > time.time()
        and authorization.get("approved_by") == "parent"
    )


class GuardedSandboxStub:
    """Refuse claims until the independently ready, live watchdog is recorded."""

    def __init__(self, stub, record, ledger, *, create_once=None):
        self.stub, self.record, self.ledger = stub, record, ledger
        self.create_once = create_once or stub.create_sandbox

    def __getattr__(self, name):
        return getattr(self.stub, name)

    def create_sandbox(self, request, **kwargs):
        remaining(self.record)
        ready = json.loads((self.ledger.parent / "cpu-watchdog-ready.json").read_text())
        if ready["phase_id"] != self.record["cpu_phase_id"]:
            raise RuntimeError("CPU watchdog phase mismatch")
        os.kill(ready["pid"], 0)
        if (self.ledger.parent / "cpu-expired").exists():
            raise TimeoutError("CPU watchdog has expired")
        rpc_seconds = min(CREATE_RPC_SECONDS, remaining(self.record))
        self.record["create_rpc_deadline_epoch"] = time.time() + rpc_seconds
        self.record["create_reconcile_until_epoch"] = (
            self.record["create_rpc_deadline_epoch"] + CREATE_RECONCILE_GRACE_SECONDS
        )
        self.record["submission_inflight"] = True
        write_json(self.ledger, self.record)
        response = self.create_once(
            request, timeout_ms=max(1, int(rpc_seconds * 1000)), **kwargs
        )
        if response.sandbox:
            self.record["seed_sandbox_id"] = response.sandbox.id
        self.record["submission_inflight"] = False
        write_json(self.ledger, self.record)
        remaining(self.record)
        return response


def recover_seed(record):
    import chalkcompute as cc

    matches = [
        item for item in cc.Sandbox.list_all() if item.name == record["seed_name"]
    ]
    if len(matches) > 1:
        raise RuntimeError("Unique CPU seed name matched multiple sandboxes")
    if not matches:
        return None
    record["seed_sandbox_id"] = matches[0].id
    return cc.Sandbox.from_id(matches[0].id)


def release_cpu(record, out):
    import chalkcompute as cc
    from chalkcompute._sandbox import SandboxNotFoundError

    guard = ResourceGuard(
        record["name"],
        out,
        record,
        record["kube_context"],
        record["fallback_kube_context"],
    )
    try:
        sandbox = (
            cc.Sandbox.from_id(record["seed_sandbox_id"])
            if record.get("seed_sandbox_id")
            else recover_seed(record)
        )
    except SandboxNotFoundError:
        sandbox = None
    if sandbox is not None:
        guard.capture_container_resource(sandbox.id, "seed_resources")
        guard.stop_sandbox(sandbox)
    if record.get("seed_sandbox_id"):
        guard.verify_container_absent(record["seed_sandbox_id"], "seed_resources")
    record["cpu_compute_released"] = True
    record["cpu_compute_released_at"] = time.time()
    guard.save()


def release_data(record):
    import chalkcompute as cc
    from chalkcompute._volume import VolumeNotFoundError

    for name in record.get("owned_volume_names", [record["name"]]):
        try:
            cc.Volume(name, create_if_missing=False).delete()
            cc.Volume(name, create_if_missing=False).reload()
        except VolumeNotFoundError:
            continue
        raise RuntimeError("CPU owned volume deletion unconfirmed")
    record["volume_verified_absent"] = True


def watchdog(ledger):
    from chalkcompute._connect import ConnectClient

    record = json.loads(ledger.read_text())
    out = ledger.parent
    phase = record["cpu_phase_id"]
    client = ConnectClient()
    client._ensure_auth()
    if client._env_id != record["environment_id"]:
        raise RuntimeError("CPU watchdog environment mismatch")
    guard = ResourceGuard(
        record["name"],
        out / "cpu-watchdog-cleanup",
        record,
        record["kube_context"],
        record["fallback_kube_context"],
    )
    guard.out.mkdir(exist_ok=True)
    for context in guard.kube_contexts:
        for resource in ("chalkcontainers", "pods"):
            guard.kube_items(resource, name="cpu-preflight-" + phase, context=context)
    write_json(
        out / "cpu-watchdog-ready.json",
        {"phase_id": phase, "pid": os.getpid(), "ready_at": time.time()},
    )
    while time.time() < record["cpu_deadline_epoch"]:
        record = json.loads(ledger.read_text())
        if (out / "cpu-watchdog-disarmed.json").exists():
            disarm = json.loads((out / "cpu-watchdog-disarmed.json").read_text())
            authorization_path = out / "cpu-authorization.json"
            authorization = (
                json.loads(authorization_path.read_text())
                if authorization_path.exists()
                else {}
            )
            released = (
                record.get("status") == "released"
                and record.get("cpu_compute_released")
                and record.get("volume_verified_absent")
            )
            if disarm.get("phase_id") != phase or not (
                released or valid_handoff(record, authorization)
            ):
                break  # Invalid handoff remains covered by cleanup.
            write_json(
                out / "cpu-watchdog-exited.json",
                {
                    "phase_id": phase,
                    "transferred": not released,
                    "exited_at": time.time(),
                },
            )
            return
        try:
            os.kill(record["launcher_pid"], 0)
        except ProcessLookupError:
            break
        time.sleep(2)
    (out / "cpu-expired").write_text(str(time.time()))
    errors = []
    history = []
    # Late ambiguous creates must be reconciled even after a first absent list.
    reconcile_until = max(
        time.time() + CREATE_RECONCILE_GRACE_SECONDS,
        record.get("create_reconcile_until_epoch", 0),
    )
    attempt = 0
    while True:
        record = json.loads(ledger.read_text())
        reconcile_until = max(
            reconcile_until, record.get("create_reconcile_until_epoch", 0)
        )
        errors = []
        try:
            release_cpu(record, guard.out)
        except Exception as error:
            errors.append(f"compute attempt {attempt}: {type(error).__name__}: {error}")
            history.extend(errors)
        attempt += 1
        if attempt >= 6 and time.time() >= reconcile_until:
            break
        time.sleep(10)
    record["create_reconciled_until_epoch"] = time.time()
    record["submission_inflight"] = False
    if not errors:
        try:
            release_data(record)
        except Exception as error:
            errors.append(f"data: {type(error).__name__}: {error}")
    write_json(
        out / "cpu-watchdog-result.json",
        {
            "phase_id": phase,
            "record": record,
            "errors": errors,
            "observation_errors": history,
            "finished_at": time.time(),
        },
    )
    if errors:
        raise RuntimeError("CPU watchdog cleanup incomplete")


SEED_CODE = r"""
import hashlib,json,math,time
from pathlib import Path
from huggingface_hub import snapshot_download
from safetensors import safe_open
from chalkcompute._weights import _TensorSpec
from chalkcompute._weights_format import architecture_hash
import xxhash
from chalkcompute.weights import WeightStream
from chalkcompute._weights_format import WeightManifest, read_pointer, version_dir
name,model,revision = CONFIG
started=time.time()
stream=WeightStream(name,model).create()
assert stream.seed_from_hf(model,revision)==0
published=time.time()
snapshot=stream._snapshot()
assert snapshot.version_id is not None
assert read_pointer(snapshot.read_file('CURRENT'))==0
manifest_bytes=snapshot.read_file(version_dir(0)+'/manifest.json')
manifest=WeightManifest.from_json(manifest_bytes)
assert manifest.kind=='keyframe' and manifest.version==0 and manifest.parent_version is None
print('CPU_SEED_ATOMIC='+json.dumps({'version':0,'snapshot_id':snapshot.version_id,'published_at':published,'seed_seconds':published-started}),flush=True)
folder=Path(snapshot_download(repo_id=model,revision=revision,local_files_only=True,allow_patterns=['*.safetensors','*.json','*.model','*.tiktoken','*.txt']))
config=snapshot.read_file(version_dir(0)+'/config.json')
assert config==(folder/'config.json').read_bytes()
catalog={}
for shard in folder.glob('*.safetensors'):
    with safe_open(shard,framework='pt',device='cpu') as handle:
        for key in handle.keys():
            tensor=handle.get_slice(key)
            assert tensor.get_dtype()=='BF16'
            catalog[key]=_TensorSpec(tuple(tensor.get_shape()),'bfloat16')
assert architecture_hash(config,list(catalog.items()))==manifest.architecture_hash
assert {t.name:(t.shape,t.dtype) for t in manifest.tensors}=={key:(value.shape,value.dtype) for key,value in catalog.items()}
files={}
for entry in manifest.tensors:
    assert entry.offset==0 and entry.encoding=='dense' and entry.nnz==math.prod(entry.shape)
    previous=files.setdefault(entry.file,(entry.length,entry.checksum))
    assert previous==(entry.length,entry.checksum)
proof_files={}
for filename,(length,checksum) in files.items():
    source=folder/filename
    source_digest=xxhash.xxh3_128()
    with source.open('rb') as handle:
        while chunk:=handle.read(4*1024*1024):
            source_digest.update(chunk)
    assert source.stat().st_size==length and source_digest.hexdigest()==checksum
    payload=snapshot.read_file(version_dir(0)+'/'+filename)
    assert len(payload)==length
    assert xxhash.xxh3_128_hexdigest(payload)==checksum
    proof_files[filename]={'bytes':len(payload),'xxh3_128':checksum}
    del payload
# Metadata is also pinned and readable from the same immutable version.
metadata={}
for file in snapshot.listdir():
    if file.kind=='file' and file.path.startswith(version_dir(0)+'/') and not file.path.endswith('.safetensors'):
        payload=snapshot.read_file(file.path)
        if not file.path.endswith('/manifest.json'):
            assert payload==(folder/file.path.split('/',1)[1]).read_bytes()
        metadata[file.path]={'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}
assert version_dir(0)+'/config.json' in metadata
proof={'volume_name':name,'model':model,'revision':revision,'current':0,'snapshot_id':snapshot.version_id,'manifest_sha256':hashlib.sha256(manifest_bytes).hexdigest(),'tensor_count':len(manifest.tensors),'elements':sum(math.prod(t.shape) for t in manifest.tensors),'files':proof_files,'metadata':metadata,'seed_seconds':published-started,'verify_seconds':time.time()-published}
print('CPU_SEED_PROOF='+json.dumps(proof),flush=True)
"""


def arm_cpu_watchdog(record, ledger):
    """Arm a detached fixed-lease guard before the first resource claim."""
    write_json(ledger, record)
    watchdog_log = (ledger.parent / "cpu-watchdog.log").open("ab", buffering=0)
    watch = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--watchdog", str(ledger)],
        stdin=subprocess.DEVNULL,
        stdout=watchdog_log,
        stderr=watchdog_log,
        start_new_session=True,
    )
    caffeinate = subprocess.Popen(
        ["caffeinate", "-i", "-w", str(watch.pid)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    record["cpu_watchdog_pid"], record["cpu_caffeinate_pid"] = watch.pid, caffeinate.pid
    write_json(ledger, record)
    for _ in range(60):
        if watch.poll() is not None:
            raise RuntimeError("CPU watchdog exited before readiness")
        if (ledger.parent / "cpu-watchdog-ready.json").exists():
            break
        time.sleep(1)
    else:
        raise TimeoutError("CPU watchdog readiness timeout; no compute submitted")

    return watch


def guarded_sandbox_client(record, ledger):
    from chalkcompute._sandbox import SandboxClient

    sandbox_client = SandboxClient.from_env()
    # This launcher sends exactly one create; it reconciles ambiguous
    # responses rather than replaying a mutating RPC. SDK policy is unchanged.
    from chalkcompute import _sandbox as sandbox_module

    create_stub = sandbox_module.pb_connect.SandboxServiceClientSync(
        sandbox_client._address,
        protocol=sandbox_module.ProtocolType.GRPC,
        interceptors=(),
        http_client=sandbox_module.SYNC_HTTP_CLIENT,
        read_max_bytes=sandbox_module.RPC_READ_MAX_BYTES,
    )
    sandbox_client._stub = GuardedSandboxStub(
        sandbox_client._stub, record, ledger, create_once=create_stub.create_sandbox
    )
    return sandbox_client


def make_guest_program(phase, code, config):
    """Inline a stdlib entrypoint; polls never invoke the supplied payload."""
    source = Path(__file__).with_name("cpu_seed_guest.py").read_text()
    root = "/tmp/weight-cpu-seed-" + phase
    entrypoint = (
        source
        + "\nrun_seed_once(Path("
        + repr(root)
        + "),"
        + repr(phase)
        + ","
        + repr(code)
        + ","
        + repr(config)
        + ")\nwhile True: time.sleep(5)\n"
    )
    poll = source + "\nprint(json.dumps(read_progress(Path(" + repr(root) + "))))\n"
    return entrypoint, poll, root


def poll_guest_until_complete(seed, record, ledger, poll_code):
    while remaining(record) > 60:
        # A replay/lost response only repeats this short read. The seed
        # process belongs to PID1, independent of any exec/RPC lifetime.
        response = seed.exec("python3", "-c", poll_code, timeout_secs=20)
        if response.error is not None or response.exit_code not in (0, None):
            raise RuntimeError("Read-only CPU progress poll failed")
        progress = json.loads(response.stdout_text)
        (ledger.parent / "seed-progress.json").write_text(
            json.dumps(progress, indent=2)
        )
        (ledger.parent / "seed-stdout.txt").write_text(progress["stdout"])
        (ledger.parent / "seed-stderr.txt").write_text(progress["stderr"])
        if progress.get("started"):
            if progress["started"]["phase_id"] != record["cpu_phase_id"]:
                raise RuntimeError("Guest entrypoint phase mismatch")
            record["seed_command_started_at"] = progress["started"]["started_at"]
        if progress.get("atomic_seed"):
            record["atomic_seed"] = progress["atomic_seed"]
            record["status"] = "proving"
        record["last_progress_at"] = time.time()
        completed = progress.get("completed")
        if completed:
            if completed["phase_id"] != record["cpu_phase_id"]:
                raise RuntimeError("Guest completion phase mismatch")
            record["seed_command_finished_at"] = completed["finished_at"]
            record["guest_completion"] = completed
            write_json(ledger, record)
            if completed["status"] != "passed":
                raise RuntimeError(
                    "CPU entrypoint seed/proof failed; see retained seed-stderr.txt"
                )
            return progress
        write_json(ledger, record)
        time.sleep(min(5, max(0, remaining(record) - 60)))
    else:
        raise TimeoutError("CPU proof did not finish before cleanup reserve")


def read_guest_file(sandbox, path, *, max_bytes, record):
    """Bounded read-only K8S file export; no filesystem sessions or mutation."""
    import base64
    from pathlib import PurePosixPath

    if not 0 <= max_bytes <= 20 * 1024 * 1024:
        raise ValueError("CPU file export bound must be 0..20MiB")
    parsed = PurePosixPath(path)
    if not parsed.is_absolute() or ".." in parsed.parts:
        raise ValueError("CPU file export requires an absolute safe guest path")

    def command(code):
        remaining(record)
        response = sandbox.exec("python3", "-c", code, timeout_secs=20)
        if response.error is not None or response.exit_code not in (0, None):
            raise RuntimeError("Read-only CPU file export failed")
        return response.stdout_text

    size = int(
        command(
            "from pathlib import Path; print(Path(" + repr(path) + ").stat().st_size)"
        )
    )
    if size > max_bytes:
        raise ValueError("Guest file exceeds authorized export bound")
    chunks = []
    for offset in range(0, size, 64 * 1024):
        code = (
            "import base64\nfrom pathlib import Path\nwith Path("
            + repr(path)
            + ').open("rb") as handle:\n handle.seek('
            + str(offset)
            + ")\n print(base64.b64encode(handle.read("
            + str(min(64 * 1024, size - offset))
            + ")).decode())\n"
        )
        chunks.append(base64.b64decode(command(code).strip(), validate=True))
    data = b"".join(chunks)
    if len(data) != size:
        raise RuntimeError("Guest file changed during bounded export")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-image", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--watchdog", type=Path)
    args = parser.parse_args()
    if args.watchdog:
        watchdog(args.watchdog)
        return
    if not all((args.prepared_image, args.env_file, args.out)):
        parser.error("--prepared-image, --env-file and --out are required")
    load_env(args.env_file)
    import chalkcompute as cc
    from chalkcompute._connect import ConnectClient

    args.out.mkdir(parents=True, exist_ok=False)
    client = ConnectClient()
    client._ensure_auth()
    from chalkcompute._gen.chalk.server.v1 import team_connect, team_pb

    environment = client.call(
        client.stub(team_connect.TeamServiceClientSync).get_env,
        team_pb.GetEnvRequest(),
    ).environment
    if environment.id != client._env_id:
        raise RuntimeError(
            "Environment metadata differs from authenticated environment"
        )
    parts = environment.kube_cluster_name.split("/")
    derived_context = "gke_" + parts[1] + "_" + parts[3] + "_" + parts[5]
    available_contexts = subprocess.check_output(
        ["kubectl", "config", "get-contexts", "-o", "name"], text=True
    ).splitlines()
    if derived_context not in available_contexts:
        raise RuntimeError("API-reported environment kube context is unavailable")
    contexts = list(
        dict.fromkeys(
            [derived_context]
            + [
                value
                for value in available_contexts
                if "chalk-serverless-stage" in value
            ]
        )
    )
    name = "weight-cpu-seed-" + uuid.uuid4().hex[:10]
    record = {
        "name": name,
        "seed_name": name + "-seed",
        "cpu_phase_id": uuid.uuid4().hex,
        "launcher_pid": os.getpid(),
        "environment_id": client._env_id,
        "expected_namespaces": [environment.id, environment.kube_job_namespace],
        "kube_context": contexts[0],
        "fallback_kube_context": contexts[-1],
        "kube_contexts": contexts,
        "status": "cpu_preparing",
        "cpu_started_at": time.time(),
        "compute_class": "K8S",
        "cpu": "2",
        "memory": "32Gi",
        "gpu_claimed": False,
    }
    record["cpu_deadline_epoch"] = record["cpu_started_at"] + LEASE_SECONDS
    preparation = json.loads((args.prepared_image.parent / "launcher.json").read_text())
    if preparation.get("status") != "cpu_prepared":
        raise RuntimeError("Require completed CPU image preparation")
    record["source_overlay_sha256"] = preparation["source_overlay_sha256"]
    record["training_deploy_image_uri"] = preparation["training_deploy_image_uri"]
    record["hf_model"], record["hf_revision"] = MODELS["8b"]
    record["prepared_image_sha256"] = hashlib.sha256(
        args.prepared_image.read_bytes()
    ).hexdigest()
    ledger = args.out / "launcher.json"
    watch = arm_cpu_watchdog(record, ledger)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    transferred = False
    try:
        sandbox_client = guarded_sandbox_client(record, ledger)
        env_keys = {
            "CHALK_API_SERVER",
            "CHALK_CLIENT_ID",
            "CHALK_CLIENT_SECRET",
            "CHALK_ACTIVE_ENVIRONMENT",
            "CHALK_ENVIRONMENT",
            "CHALK_ENVIRONMENT_ID",
        }
        remote_env = {
            key: value for key, value in os.environ.items() if key in env_keys
        }
        remote_env.update(
            {"PYTHONUNBUFFERED": "1", "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2"}
        )
        model, revision = MODELS["8b"]
        guest_program, poll_code, guest_root = make_guest_program(
            record["cpu_phase_id"], SEED_CODE, (name, model, revision)
        )
        (args.out / "guest-entrypoint-executed.py").write_text(guest_program)
        record["guest_entrypoint_sha256"] = hashlib.sha256(
            guest_program.encode()
        ).hexdigest()
        record["guest_root"] = guest_root
        write_json(ledger, record)
        seed = cc.Sandbox(
            name=record["seed_name"],
            image=cc.Image.from_json(args.prepared_image.read_text()),
            entrypoint=["python3", "-u", "-c", guest_program],
            restart_policy=cc.RestartPolicy.NEVER,
            cpu="2",
            memory="32Gi",
            compute_class=cc.ComputeClass.K8S,
            execution_target=cc.ExecutionBackend.SERVERLESS,
            lifetime="1200s",
            env=remote_env,
        )
        sandbox_client.run(seed, timeout=min(STARTUP_SECONDS, remaining(record)))
        record["cpu_ready_at"] = time.time()
        record["status"] = "seeding"
        write_json(ledger, record)
        progress = poll_guest_until_complete(seed, record, ledger, poll_code)
        proof = progress["proof"]
        write_json(args.out / "cpu-proof.json", proof)
        record["snapshot_id"] = proof["snapshot_id"]
        record["proof_sha256"] = hashlib.sha256(
            (args.out / "cpu-proof.json").read_bytes()
        ).hexdigest()
        release_cpu(record, args.out)
        record["status"] = "paused_proof"
        write_json(ledger, record)
        print(
            json.dumps(
                {
                    "status": "paused_proof",
                    "ledger": str(ledger),
                    "deadline": record["cpu_deadline_epoch"],
                    "proof_sha256": record["proof_sha256"],
                }
            ),
            flush=True,
        )
        while remaining(record) > 0:
            authorization_path = args.out / "cpu-authorization.json"
            if authorization_path.exists():
                authorization = json.loads(authorization_path.read_text())
                if not valid_handoff(record, authorization):
                    raise RuntimeError(
                        "CPU authorization does not bind this proof/phase and a ready GPU guard"
                    )
                write_json(
                    args.out / "cpu-watchdog-disarmed.json",
                    {"phase_id": record["cpu_phase_id"]},
                )
                watch.wait(timeout=15)
                if (
                    watch.returncode
                    or not (args.out / "cpu-watchdog-exited.json").exists()
                ):
                    raise RuntimeError("CPU watchdog exit unconfirmed")
                transferred = True
                record["status"] = "data_ownership_transferred"
                record["gpu_phase_id"] = authorization["gpu_phase_id"]
                write_json(ledger, record)
                return
            time.sleep(1)
    except BaseException as error:
        record["failure"] = {
            "type": type(error).__name__,
            "message": str(error),
            "at": time.time(),
        }
        write_json(ledger, record)
        raise
    finally:
        if not transferred:
            # The detached watchdog remains armed if local cleanup fails.
            try:
                release_cpu(record, args.out)
                if record.get("submission_inflight"):
                    raise RuntimeError(
                        "Ambiguous CPU create; detached reconciliation remains armed"
                    )
                release_data(record)
                record["status"] = "released"
                write_json(ledger, record)
                write_json(
                    args.out / "cpu-watchdog-disarmed.json",
                    {"phase_id": record["cpu_phase_id"]},
                )
                watch.wait(timeout=15)
                if (
                    watch.returncode
                    or not (args.out / "cpu-watchdog-exited.json").exists()
                ):
                    raise RuntimeError("CPU cleanup watchdog exit unconfirmed")
            finally:
                print(
                    json.dumps(
                        {
                            "status": record["status"],
                            "cpu_deadline": record["cpu_deadline_epoch"],
                        }
                    ),
                    flush=True,
                )


if __name__ == "__main__":
    main()
