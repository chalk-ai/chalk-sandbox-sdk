"""Freeze/review a separate tiny synthetic CPU plan; never allocates resources."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import profile_plan as full
from profile_plan import sha, write_json
from profile_plan import activate_controller_sdk as activate_controller_sdk

NEW_SOURCES = (
    "profile_streaming.py",
    "profile_sharded.py",
    "profile_sharded_metrics.py",
    "profile_sharded_guest.py",
    "profile_sharded_remote.py",
    "profile_sharded_guard.py",
    "profile_sharded_plan.py",
    "profile_sharded_verify.py",
)
POLICY = {
    "base_profile_plan_sha256": "94d331f3509a50d1ce5d0eb4e7dc1f987151311f931e12f7bd8ab2a0798c06f9",
    "kind": "sharded_synthetic_sdk_pipeline_v1",
    "cpu_seconds": 600,
    "cpu_cleanup_reserve_seconds": 120,
    "cpu": "4",
    "memory": "8Gi",
    "max_gpu": 0,
    "max_a100": 0,
    "max_amd": 0,
    "fixture_source_bytes": 16 * 1024**2,
    "maximum_total_fixture_bytes": 256 * 1024**2,
    "minimum_host_free_disk_bytes": 2 * 1024**3,
    "minimum_guest_free_disk_bytes": 512 * 1024**2,
    "host_id": "svlsbfe05ebc-oracle-a100",
    "host_pool_id": "ExternalClusterHosts",
    "image_uri": "us-docker.pkg.dev/chalk-develop/compute/compute-demo@sha256:4db9af506dee49da3d355959da1e98905934b7d349716f77f00b96359718f2d7",
    "allocations_made": False,
    "full_8b_projection_measured": False,
    "current_frozen_profile_gate_changed": False,
    "requires_parent_review_before_allocation": True,
}


def freeze(out, previous):
    if sha(previous / "plan.json") != POLICY["base_profile_plan_sha256"]:
        raise ValueError("Require the independently reviewed immutable v4 package")
    baseline = full.validate(previous)
    if baseline["image_uri"] != POLICY["image_uri"]:
        raise ValueError("Reviewed immutable image differs")
    out.mkdir(parents=True, exist_ok=False)
    for name in ("guard", "controller-sdk"):
        shutil.copytree(
            previous / name,
            out / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    for name in ("image.json", "image-provenance.json", "runtime.tar.gz"):
        shutil.copyfile(previous / name, out / name)
    for name in NEW_SOURCES:
        shutil.copyfile(Path(__file__).with_name(name), out / "guard" / name)
    plan = {
        **POLICY,
        "base_profile_plan_sha256": sha(previous / "plan.json"),
        "image_json_sha256": sha(out / "image.json"),
        "image_provenance_sha256": sha(out / "image-provenance.json"),
        "overlay_sha256": sha(out / "runtime.tar.gz"),
        "runtime_sources": baseline["runtime_sources"],
        "controller_sdk_sources": baseline["controller_sdk_sources"],
        "guard_sources": {p.name: sha(p) for p in sorted((out / "guard").glob("*.py"))},
    }
    write_json(out / "plan.json", plan)
    validate(out)
    return plan


def validate(out):
    import hashlib
    import tarfile

    plan = json.loads((out / "plan.json").read_bytes())
    if any(plan.get(k) != v for k, v in POLICY.items()):
        raise ValueError("Small synthetic fixed resource policy differs")
    expected = set((*full.HELPERS, *full.OWN_SOURCES, *NEW_SOURCES))
    guards = {
        p.name: sha(p) for p in (out / "guard").glob("*.py") if not p.is_symlink()
    }
    if set(guards) != expected or guards != plan["guard_sources"]:
        raise ValueError("Frozen small-fixture helper source differs")
    controller = {
        p.relative_to(out / "controller-sdk").as_posix(): sha(p)
        for p in (out / "controller-sdk").rglob("*.py")
        if not p.is_symlink()
    }
    if controller != plan["controller_sdk_sources"]:
        raise ValueError("Frozen small-fixture controller SDK differs")
    for name, key in (
        ("runtime.tar.gz", "overlay_sha256"),
        ("image.json", "image_json_sha256"),
        ("image-provenance.json", "image_provenance_sha256"),
    ):
        if sha(out / name) != plan[key]:
            raise ValueError("Frozen overlay/image identity differs")
    image = json.loads((out / "image.json").read_bytes())
    if image != {"image_uri": POLICY["image_uri"], "schema_version": 1, "volumes": []}:
        raise ValueError("Require exact immutable mount-free image")
    with tarfile.open(out / "runtime.tar.gz") as archive:
        actual = {}
        for member in archive.getmembers():
            if not member.isfile():
                raise ValueError("Require source-only runtime overlay")
            reader = archive.extractfile(member)
            assert reader is not None
            actual[member.name] = hashlib.sha256(reader.read()).hexdigest()
    if actual != plan["runtime_sources"]:
        raise ValueError("Frozen runtime source catalog differs")
    return plan


def require_authorization(out, authorization, action):
    plan = validate(out)
    if not (
        action == "execute_sharded_synthetic_cpu"
        and authorization
        == {
            "approved_by": "parent",
            "action": action,
            "plan_sha256": sha(out / "plan.json"),
            "source_policy": "frozen-plan-only",
        }
    ):
        raise ValueError("Require parent approval of exact small-fixture plan/action")
    full.activate_controller_sdk(out)
    return plan


def validate_ledger(ledger):
    plan = validate(ledger.parent)
    record = json.loads(ledger.read_bytes())
    if (
        record.get("plan_sha256") != sha(ledger.parent / "plan.json")
        or record.get("cpu_deadline_epoch", 0) != record.get("cpu_started_at", 0) + 600
        or record.get("cpu") != plan["cpu"]
        or record.get("memory") != plan["memory"]
        or record.get("gpu_claimed") is not False
        or record.get("launcher_identity", {}).get("pid") != record.get("launcher_pid")
    ):
        raise RuntimeError("Synthetic ledger differs from frozen fixed lease/resources")
    return record


def validate_capacity(value):
    if value["memory"]["MemAvailable"] < 8 * 1024**3 or value["cpu_affinity_count"] < 4:
        raise RuntimeError("Small synthetic host CPU/memory headroom insufficient")
    if (
        value["filesystems"]["/var/lib/docker"]["free_bytes"]
        < POLICY["minimum_host_free_disk_bytes"]
    ):
        raise RuntimeError("Small synthetic host disk headroom insufficient")
    limits = value.get("cgroup_v2")
    if not isinstance(limits, list) or not limits:
        raise RuntimeError("Missing host ancestor cgroup capacity proof")
    for item in limits:
        maximum, used = item.get("memory.max"), item.get("memory.current")
        if (
            maximum is not None
            and maximum != "max"
            and (used is None or int(maximum) - int(used) < 8 * 1024**3)
        ):
            raise RuntimeError("Small synthetic ancestor memory headroom insufficient")
        quota = item.get("cpu.max")
        if quota:
            amount, period = quota.split()
            if amount != "max" and int(amount) < 4 * int(period):
                raise RuntimeError("Small synthetic ancestor CPU quota insufficient")
    return value


def runtime_capacity_code():
    from profile_capacity import CAPACITY_CODE

    # Reuse the read-only ancestor walk, adding the actual kernel peak counter.
    return CAPACITY_CODE.replace(
        "'memory.current','cpu.max'", "'memory.current','memory.peak','cpu.max'"
    )


def validate_runtime_limits(value):
    ancestors = value.get("cgroup_v2", [])
    bounded = [
        item for item in ancestors if item.get("memory.max") not in (None, "max")
    ]
    if not bounded or min(int(item["memory.max"]) for item in bounded) > 8 * 1024**3:
        raise RuntimeError("Owned runtime memory limit exceeds8GiB or is unproved")
    if not any("memory.peak" in item and "memory.current" in item for item in bounded):
        raise RuntimeError("Owned runtime memory current/peak counters unavailable")
    cpu_limits = [
        int(item["cpu.max"].split()[0]) / int(item["cpu.max"].split()[1])
        for item in ancestors
        if item.get("cpu.max", "max").split()[0] != "max"
    ]
    if value["cpu_affinity_count"] > 4 and (not cpu_limits or min(cpu_limits) > 4):
        raise RuntimeError("Owned runtime CPU bound exceeds4CPU or is unproved")
    return value


def preview(out):
    import shlex
    from profile_sharded_remote import build_guest_program

    plan = validate(out)
    program, _, _ = build_guest_program(
        out, plan, "weight-profile-0000000000", "0" * 32, 2000000600
    )
    compile(program, "sharded-synthetic-preview", "exec")
    (out / "cpu-entrypoint-preview.py").write_text(program)
    commands = {
        "validate": [
            sys.executable,
            str(out / "guard/profile_sharded_plan.py"),
            "--out",
            str(out),
            "--validate",
        ],
        "execute": [
            sys.executable,
            str(out / "guard/profile_sharded_remote.py"),
            "--plan",
            str(out),
            "--authorization",
            str(out / "cpu-execution-authorization.json"),
            "--env-file",
            "<FTQA_ENV_FILE>",
        ],
        "cleanup": [
            sys.executable,
            str(out / "guard/profile_sharded_guard.py"),
            "--cleanup",
            str(out / "launcher.json"),
            "--env-file",
            "<FTQA_ENV_FILE>",
        ],
        "verify": [
            sys.executable,
            str(out / "guard/profile_sharded_verify.py"),
            "--cpu-ledger",
            str(out / "launcher.json"),
            "--out",
            str(out / "independent-release"),
            "--env-file",
            "<FTQA_ENV_FILE>",
        ],
    }
    write_json(
        out / "preview.json",
        {
            "plan_sha256": sha(out / "plan.json"),
            "entrypoint_bytes": len(program.encode()),
            "commands": {k: shlex.join(v) for k, v in commands.items()},
            "allocations_made": False,
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--from-frozen", type=Path)
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()
    if args.from_frozen:
        freeze(args.out, args.from_frozen)
    elif args.validate:
        validate(args.out)
    elif args.preview:
        preview(args.out)
    else:
        parser.error("Require from-frozen, validate, or preview")
