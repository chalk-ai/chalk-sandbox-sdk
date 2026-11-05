"""Pure construction and bounded internal proof probes for owned profile guests."""

from __future__ import annotations

import base64
import json

from profile_fixture import AUDIT_NAMES, require_target_changed, verify_native


def serving_program(overlay):
    encoded = base64.b64encode(overlay).decode()
    return f"""import base64,importlib.util,io,os,sys,tarfile
from pathlib import Path
spec=importlib.util.find_spec('chalkcompute')
assert spec and spec.origin
with tarfile.open(fileobj=io.BytesIO(base64.b64decode({encoded!r}))) as archive:
 archive.extractall(Path(spec.origin).parent.parent,filter='data')
log=open('/tmp/weight-profile-runtime.log','ab',buffering=0)
os.dup2(log.fileno(),1);os.dup2(log.fileno(),2)
os.execv(sys.executable,[sys.executable,'-m','chalkcompute.weights.serving','--weights','/chalk/weights','--startup-timeout','480','--','--served-model-name','policy','--dtype','bfloat16','--max-model-len','2048','--gpu-memory-utilization','.5','--enforce-eager'])
"""


def validate_cpu_proof(record, proof, plan):
    if not (
        record["status"] == "paused_proof"
        and record.get("cpu_compute_released") is True
        and record.get("seed_resources_verified_absent") is True
        and record.get("host_runtime_verified_absent") is True
        and proof.get("tensor_count") == 399
        and proof.get("elements") == 8190735360
        and proof.get("actual_changed_elements") == 81907353
        and proof.get("whole_model_cpu_bytes_equal") is True
        and proof.get("current") == 1
        and proof.get("pin") == 0
        and proof.get("reference_current") == 0
        and proof.get("revision") == plan["revision"]
        and proof.get("model") == plan["model"]
        and proof.get("memory_estimate", {}).get("fits_80gib_with_reserve") is True
        and proof.get("volume_name") == record["name"]
        and proof.get("reference_volume_name") == record["name"] + "-reference"
        and record["owned_volume_names"]
        == [record["name"], record["name"] + "-reference"]
    ):
        raise ValueError("CPU proof/release/config/memory gate is incomplete")
    markers = record.get("volume_creation_markers", {})
    if set(markers) != set(record["owned_volume_names"]) or any(
        m.get("status") != "acknowledged"
        or m.get("phase_id") != record["cpu_phase_id"]
        or m.get("name") != n
        for n, m in markers.items()
    ):
        raise ValueError("CPU metadata create ownership acknowledgements incomplete")
    counts = proof.get("per_tensor_changed_elements", {})
    if (
        len(counts) != 399
        or any(type(v) is not int or v <= 0 for v in counts.values())
        or sum(counts.values()) != 81907353
    ):
        raise ValueError("Require exact global1% and per-tensor change distribution")
    require_target_changed(
        proof["base_three_parameter_hashes"], proof["target_three_parameter_hashes"]
    )
    if (
        proof.get("snapshot_id") != record["snapshot_id"]
        or proof.get("reference_snapshot_id") != record["reference_snapshot_id"]
    ):
        raise ValueError("Immutable CPU snapshots changed")
    if (
        not proof.get("workspace", {}).get("quota_reservation_released")
        or proof["workspace"].get("quota_reservation_bytes_verified", 0) < 80 * 1024**3
    ):
        raise ValueError("Guest workspace quota proof is incomplete")
    return proof


def native_audit(replica, expected, host):
    # Caller verifies exact live owner/UID/placement before this read-only RPC.
    code = """import json
from urllib.request import Request,urlopen
request=Request('http://127.0.0.1:8001/collective_rpc',data=json.dumps({'method':'ws_tensor_hashes','args':[NAMES]}).encode(),headers={'Content-Type':'application/json'},method='POST')
with urlopen(request,timeout=45) as response:
 data=response.read(65537)
 assert len(data)<=65536
 print(data.decode())
""".replace("NAMES", repr(list(AUDIT_NAMES)))
    command = [
        "sudo",
        "-n",
        "docker",
        "exec",
        "chalk-external-hypervisor",
        "runsc",
        "--root=/var/lib/chalk-hypervisor/runsc",
        "exec",
        replica,
        "python3",
        "-c",
        code,
    ]
    value = json.loads(host.ssh(command))
    verify_native(value, expected)
    return value


def profile_events(replica, host):
    code = """import json
from pathlib import Path
path=Path('/tmp/weight-profile-runtime.log')
with path.open('rb') as f:
 f.seek(max(0,path.stat().st_size-1024*1024)); data=f.read(1024*1024)
result=[]
for line in data.decode(errors='replace').splitlines():
 try:
  item=json.loads(line[line.index('{'):])
  if item.get('event')=='chalk_weight_sync_profile': result.append(item)
 except (ValueError,TypeError): pass
text=json.dumps(result)
assert len(text.encode())<=256*1024
print(text)
"""
    return json.loads(
        host.ssh(
            [
                "sudo",
                "-n",
                "docker",
                "exec",
                "chalk-external-hypervisor",
                "runsc",
                "--root=/var/lib/chalk-hypervisor/runsc",
                "exec",
                replica,
                "python3",
                "-c",
                code,
            ]
        )
    )


def validate_profile_events(events):
    """Require complete apply/cache/sync telemetry for target and restoration."""
    import math

    if not isinstance(events, list):
        raise ValueError("Missing bounded profile events")
    expected = {
        "apply": {"collective_rpc_host_ms"},
        "cache_reset": {"host_ms"},
        "sync": {
            "reload_host_ms",
            "verify_host_ms",
            "apply_ms",
            "pause_ms",
            "drain_host_ms",
        },
    }
    result = {}
    for version in (1, 0):
        result[str(version)] = {}
        for phase, keys in expected.items():
            matches = [
                e
                for e in events
                if e.get("version") == version and e.get("phase") == phase
            ]
            if len(matches) != 1:
                raise ValueError("Missing or repeated target/restore profiling phase")
            event = matches[0]
            if event.get("event") != "chalk_weight_sync_profile" or any(
                type(event.get(k)) not in (int, float)
                or not math.isfinite(event[k])
                or event[k] < 0
                for k in keys
            ):
                raise ValueError("Invalid finite profiling timings")
            if phase == "apply":
                workers = validate_worker_profile(event.get("workers"), version)
                if (
                    workers[0]["device"] != "cuda:0"
                    or workers[0]["counts"]["tensors"] != 399
                    or workers[0]["counts"]["payload_bytes"] <= 0
                ):
                    raise ValueError("Require full-model single CUDA worker profiling")
            if phase == "sync" and event.get("from_version") != 1 - version:
                raise ValueError("Profile transition differs from target/restore")
            result[str(version)][phase] = event
    return result


def require_zero_failures(group):
    response = group.call("/chalk/metrics", method="GET", timeout=10)
    if response.status != 200:
        raise RuntimeError("Cannot read final serving failure metric")
    text = (
        response.content.decode()
        if isinstance(response.content, bytes)
        else response.content
    )
    samples = [
        line.split()
        for line in text.splitlines()
        if line.startswith("chalk_weight_sync_failures ")
    ]
    if samples != [["chalk_weight_sync_failures", "0"]]:
        raise RuntimeError("Serving sync failure counter is missing or nonzero")
    return {"chalk_weight_sync_failures": 0}


def validate_worker_profile(workers, version):
    """Independent receiver schema check, usable without locally installing overlay."""
    import math

    if not isinstance(workers, list) or len(workers) != 1:
        raise ValueError("Require exactly one native worker profile")
    worker = workers[0]
    if not isinstance(worker, dict) or set(worker) != {
        "schema_version",
        "version",
        "rank",
        "local_rank",
        "worker_pid",
        "device",
        "host_ms",
        "stream_elapsed_ms",
        "counts",
    }:
        raise ValueError("Unexpected native worker profile schema")
    for field, expected in (
        ("schema_version", 1),
        ("version", version),
        ("rank", 0),
        ("local_rank", 0),
    ):
        if type(worker[field]) is not int or worker[field] != expected:
            raise ValueError("Native worker profile identity/version differs")
    if (
        type(worker["worker_pid"]) is not int
        or worker["worker_pid"] <= 0
        or worker["device"] != "cuda:0"
    ):
        raise ValueError("Native CUDA worker profile identity missing")
    host = {
        "manifest",
        "verify",
        "read_checksum",
        "decode",
        "h2d_host",
        "dense_load_host",
        "sparse_load_host",
        "synchronize_host",
    }
    stream = {"h2d_host", "dense_load_host", "sparse_load_host"}
    for field, keys, exact in (
        ("host_ms", host, True),
        ("stream_elapsed_ms", stream, False),
    ):
        values = worker[field]
        if (
            not isinstance(values, dict)
            or (set(values) != keys if exact else not set(values) <= keys)
            or any(
                type(v) not in (int, float) or not math.isfinite(v) or v < 0
                for v in values.values()
            )
        ):
            raise ValueError("Native worker timings invalid/nonfinite")
    counts = worker["counts"]
    if (
        not isinstance(counts, dict)
        or set(counts)
        != {"payload_bytes", "value_bytes", "index_bytes", "tensors", "sparse_batches"}
        or any(type(v) is not int or v < 0 for v in counts.values())
    ):
        raise ValueError("Native worker byte/tensor counters invalid")
    return workers
