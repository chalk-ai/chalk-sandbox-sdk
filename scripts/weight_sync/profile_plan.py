"""Prepare and validate a frozen profiling plan locally; never claims compute."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import time
import tarfile
from pathlib import Path

MODEL = "Qwen/Qwen3-8B"
REVISION = "b968826d9c46dd6066d109eabc6255188de91218"
CPU_SECONDS = GPU_SECONDS = 1800
HOST = "svlsbfe05ebc-oracle-a100"
POOL = "ExternalClusterHosts"
HELPERS = (
    "cpu_seed.py",
    "cpu_seed_guest.py",
    "training_smoke.py",
    "ftqa_smoke.py",
    "seed_handoff.py",
)
OWN_SOURCES = (
    "profile_fixture.py",
    "profile_cpu_payload.py",
    "profile_host_guard.py",
    "profile_cpu.py",
    "profile_plan.py",
    "profile_volumes.py",
    "profile_receiver.py",
    "profile_gpu_guard.py",
    "profile_gpu_payload.py",
    "profile_rpc.py",
    "profile_writers.py",
    "profile_verify.py",
)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, data):
    temporary = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    with temporary.open("w") as handle:
        handle.write(json.dumps(data, indent=2))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def resolve_image(image_json, out):
    image = json.loads(image_json.read_bytes())
    tag = image["image_uri"]
    if not isinstance(tag, str) or not re.fullmatch(
        r"us-docker\.pkg\.dev/chalk-develop/compute/compute-demo:[a-z0-9]+", tag
    ):
        raise ValueError("Require the reviewed image tag")
    result = subprocess.run(
        [
            "gcloud",
            "artifacts",
            "docker",
            "images",
            "describe",
            tag,
            "--format=json(image_summary.digest)",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    )
    digest = json.loads(result.stdout)["image_summary"]["digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Registry returned an invalid image digest")
    value = {
        "source_tag": tag,
        "image_uri": tag.rsplit(":", 1)[0] + "@" + digest,
        "resolved_digest": digest,
        "checked_at": time.time(),
        "resolver": "gcloud artifacts docker images describe",
    }
    write_json(out, value)
    return value


def prepare(
    out, runtime_root, guard_root, image_json, provenance_json, controller_root
):
    out.mkdir(parents=True, exist_ok=False)
    image = json.loads(image_json.read_bytes())
    if (
        set(image) != {"image_uri", "schema_version", "volumes"}
        or image["schema_version"] != 1
        or image["volumes"]
    ):
        raise ValueError("Require a previously built immutable image without mounts")
    if not isinstance(image["image_uri"], str) or not image["image_uri"].startswith(
        "us-docker.pkg.dev/chalk-develop/compute/compute-demo:"
    ):
        raise ValueError("Require the reviewed prebuilt Chalk vLLM image")
    provenance = json.loads(provenance_json.read_bytes())
    if provenance.get("source_tag") != image["image_uri"] or not re.fullmatch(
        r"us-docker\.pkg\.dev/chalk-develop/compute/compute-demo@sha256:[0-9a-f]{64}",
        provenance.get("image_uri", ""),
    ):
        raise ValueError("Require registry-resolved immutable image provenance")
    if provenance.get("resolved_digest") != provenance["image_uri"].rsplit("@", 1)[1]:
        raise ValueError("Resolved digest and image content identity differ")
    image["image_uri"] = provenance["image_uri"]
    (out / "image-provenance.json").write_bytes(provenance_json.read_bytes())
    sources = {}
    frozen = out / "guard"
    frozen.mkdir()
    for name in (*HELPERS, *OWN_SOURCES):
        source = (guard_root if name in HELPERS else Path(__file__).parent) / name
        if not source.is_file() or source.is_symlink():
            raise ValueError("Freeze ordinary helper source files only")
        target = frozen / name
        target.write_bytes(source.read_bytes())
        sources[name] = sha(target)
    controller = {}
    controller_target = out / "controller-sdk"
    for source in sorted((controller_root / "chalkcompute").rglob("*.py")):
        if not source.is_file() or source.is_symlink():
            raise ValueError("Controller SDK requires ordinary source files")
        relative = source.relative_to(controller_root)
        target = controller_target / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        controller[relative.as_posix()] = sha(target)
    if not controller:
        raise ValueError("Controller SDK sources are empty")
    write_json(out / "image.json", image)
    package = runtime_root / "chalkcompute"
    paths = [
        package / name
        for name in ("_weights.py", "_weights_codec.py", "_weights_format.py")
    ]
    paths.extend(sorted((package / "weights").rglob("*.py")))
    if not (package / "weights/serving/profiling.py").is_file():
        raise ValueError("Runtime must contain reviewed profiling support")
    archive = io.BytesIO()
    runtime = {}
    with tarfile.open(fileobj=archive, mode="w:gz") as target:
        for source in paths:
            if not source.is_file() or source.is_symlink():
                raise ValueError("Runtime overlay requires ordinary source files")
            relative = source.relative_to(runtime_root).as_posix()
            runtime[relative] = sha(source)
            target.add(source, arcname=relative, recursive=False)
    if len(archive.getvalue()) > 64 * 1024:
        raise ValueError("Runtime overlay exceeds inline64KiB bound")
    (out / "runtime.tar.gz").write_bytes(archive.getvalue())
    plan = {
        "kind": "weight_profile_8b_one_percent_v1",
        "model": MODEL,
        "revision": REVISION,
        "cpu_seconds": CPU_SECONDS,
        "gpu_seconds": GPU_SECONDS,
        "cpu": "16",
        "memory": "128Gi",
        "cpu_compute_class": "HOST",
        "host_id": HOST,
        "host_pool_id": POOL,
        "max_a100": 1,
        "max_amd": 0,
        "expected_tensors": 399,
        "expected_elements": 8190735360,
        "expected_changed_elements": 81907353,
        "hf_cache": "/tmp/weight-profile-hf-cache",
        "host_cache_mounts": [],
        "minimum_host_free_disk_bytes": 120 * 1024**3,
        "engine_memory_utilization": 0.5,
        "minimum_gpu_free_reserve_bytes": 8 * 1024**3,
        "native_audit_scope": "three-parameter audit; whole-model CPU proof separately",
        "image_uri": image["image_uri"],
        "image_json_sha256": sha(out / "image.json"),
        "image_provenance_sha256": sha(out / "image-provenance.json"),
        "overlay_sha256": sha(out / "runtime.tar.gz"),
        "runtime_sources": runtime,
        "controller_sdk_sources": controller,
        "guard_sources": sources,
        "allocations_made": False,
        "requires_parent_review_before_cpu_or_gpu_allocation": True,
    }
    write_json(out / "plan.json", plan)
    return plan


def validate(out):
    plan = json.loads((out / "plan.json").read_bytes())
    fixed = {
        "kind": "weight_profile_8b_one_percent_v1",
        "model": MODEL,
        "revision": REVISION,
        "cpu_seconds": CPU_SECONDS,
        "gpu_seconds": GPU_SECONDS,
        "cpu": "16",
        "memory": "128Gi",
        "cpu_compute_class": "HOST",
        "host_id": HOST,
        "host_pool_id": POOL,
        "max_a100": 1,
        "max_amd": 0,
        "expected_tensors": 399,
        "expected_elements": 8190735360,
        "expected_changed_elements": 81907353,
        "hf_cache": "/tmp/weight-profile-hf-cache",
        "host_cache_mounts": [],
        "minimum_host_free_disk_bytes": 120 * 1024**3,
        "engine_memory_utilization": 0.5,
        "minimum_gpu_free_reserve_bytes": 8 * 1024**3,
        "allocations_made": False,
        "requires_parent_review_before_cpu_or_gpu_allocation": True,
    }
    if any(plan.get(key) != value for key, value in fixed.items()):
        raise ValueError("Frozen profiling resource/fixture policy changed")
    if (
        sha(out / "image.json") != plan["image_json_sha256"]
        or sha(out / "runtime.tar.gz") != plan["overlay_sha256"]
    ):
        raise ValueError("Frozen image/runtime changed")
    provenance = json.loads((out / "image-provenance.json").read_bytes())
    image = json.loads((out / "image.json").read_bytes())
    if (
        sha(out / "image-provenance.json") != plan["image_provenance_sha256"]
        or image["image_uri"] != plan["image_uri"]
        or provenance["image_uri"] != plan["image_uri"]
        or provenance.get("resolved_digest") != plan["image_uri"].rsplit("@", 1)[1]
        or not re.fullmatch(
            r"us-docker\.pkg\.dev/chalk-develop/compute/compute-demo@sha256:[0-9a-f]{64}",
            plan["image_uri"],
        )
    ):
        raise ValueError("Resolved image content identity changed")
    if set(plan["guard_sources"]) != set((*HELPERS, *OWN_SOURCES)) or any(
        sha(out / "guard" / name) != value
        for name, value in plan["guard_sources"].items()
    ):
        raise ValueError("Frozen CPU guard/payload changed")
    actual_controller = {
        p.relative_to(out / "controller-sdk").as_posix(): sha(p)
        for p in (out / "controller-sdk").rglob("*.py")
        if p.is_file() and not p.is_symlink()
    }
    if actual_controller != plan["controller_sdk_sources"]:
        raise ValueError("Frozen local controller SDK changed")
    with tarfile.open(out / "runtime.tar.gz") as archive:
        actual = {}
        for member in archive.getmembers():
            if not member.isfile():
                raise ValueError("Runtime archive contains a non-source member")
            source = archive.extractfile(member)
            assert source is not None
            actual[member.name] = hashlib.sha256(source.read()).hexdigest()
    if actual != plan["runtime_sources"]:
        raise ValueError("Runtime source catalog changed")
    return plan


def require_authorization(out, authorization, action):
    plan = validate(out)
    if not (
        authorization.get("approved_by") == "parent"
        and authorization.get("action") == action
        and authorization.get("plan_sha256") == sha(out / "plan.json")
        and authorization.get("source_policy") == "frozen-plan-only"
    ):
        raise ValueError("Parent authorization must bind exact plan and action")
    activate_controller_sdk(out)
    return plan


def activate_controller_sdk(out):
    import sys

    root = str(out / "controller-sdk")
    loaded = sys.modules.get("chalkcompute")
    if loaded is not None and (
        loaded.__file__ is None
        or not str(Path(loaded.__file__).resolve()).startswith(root + "/")
    ):
        raise RuntimeError(
            "Controller SDK was imported before frozen-source activation"
        )
    sys.path.insert(0, root)
    os.environ["PYTHONPATH"] = root


def preview(out):
    """Produce exact source/argument shapes without auth, image pulls or resources."""
    import shlex
    import sys

    plan = validate(out)
    sys.path.insert(0, str(out / "guard"))
    from profile_cpu import build_guest_program
    from profile_gpu_payload import serving_program

    cpu, _, _ = build_guest_program(out, plan, "weight-profile-0000000000", "0" * 32)
    gpu = serving_program((out / "runtime.tar.gz").read_bytes())
    compile(cpu, "profile-cpu-preview", "exec")
    compile(gpu, "profile-gpu-preview", "exec")
    if any(len(p.encode()) > 120 * 1024 for p in (cpu, gpu)):
        raise ValueError("Preview exceeds reviewed guest argument limit")
    (out / "cpu-entrypoint-preview.py").write_text(cpu)
    (out / "gpu-entrypoint-preview.py").write_text(gpu)
    commands = {
        "validate": [
            sys.executable,
            str(out / "guard/profile_plan.py"),
            "--out",
            str(out),
            "--validate",
        ],
        "cpu": [
            sys.executable,
            str(out / "guard/profile_cpu.py"),
            "--plan",
            str(out),
            "--authorization",
            str(out / "cpu-execution-authorization.json"),
            "--env-file",
            "<FTQA_ENV_FILE>",
        ],
        "cleanup_proof": [
            sys.executable,
            str(out / "guard/profile_verify.py"),
            "--cpu-ledger",
            str(out / "launcher.json"),
            "--gpu-ledger",
            str(out / "gpu/launcher.json"),
            "--out",
            str(out / "independent-cleanup-proof"),
            "--env-file",
            "<FTQA_ENV_FILE>",
        ],
        "gpu": [
            sys.executable,
            str(out / "guard/profile_receiver.py"),
            "--plan",
            str(out),
            "--out",
            str(out / "gpu"),
            "--authorization",
            str(out / "gpu-execution-authorization.json"),
            "--env-file",
            "<FTQA_ENV_FILE>",
        ],
    }
    result = {
        "plan_sha256": sha(out / "plan.json"),
        "cpu_entrypoint_bytes": len(cpu.encode()),
        "gpu_entrypoint_bytes": len(gpu.encode()),
        "cpu_entrypoint_sha256": sha(out / "cpu-entrypoint-preview.py"),
        "gpu_entrypoint_sha256": sha(out / "gpu-entrypoint-preview.py"),
        "commands": {k: shlex.join(v) for k, v in commands.items()},
        "allocations_made": False,
        "authorization_files_created": False,
    }
    write_json(out / "preview.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--controller-root", type=Path)
    parser.add_argument("--guard-root", type=Path)
    parser.add_argument("--image-json", type=Path)
    parser.add_argument("--image-provenance", type=Path)
    parser.add_argument("--resolve-image", action="store_true")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()
    if args.resolve_image:
        if not args.image_json:
            parser.error("resolve-image requires image-json")
        print(json.dumps(resolve_image(args.image_json, args.out), indent=2))
        return
    if args.preview:
        print(json.dumps(preview(args.out.resolve()), indent=2))
        return
    if args.validate:
        plan = validate(args.out)
    else:
        if not all(
            (
                args.runtime_root,
                args.guard_root,
                args.image_json,
                args.image_provenance,
                args.controller_root,
            )
        ):
            parser.error("Preparation requires runtime-root, guard-root and image-json")
        plan = prepare(
            args.out,
            args.runtime_root,
            args.guard_root,
            args.image_json,
            args.image_provenance,
            args.controller_root,
        )
    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
