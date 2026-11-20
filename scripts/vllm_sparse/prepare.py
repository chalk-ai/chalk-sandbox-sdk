"""Local deterministic minimal overlay preparation; never creates cloud resources."""

import argparse
import gzip
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

from scripts.vllm_sparse.proof import selected_architecture_hash
from scripts.vllm_sparse.runtime_gate import REVIEWED_VLLM_VERSIONS


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--audit-root", type=Path, required=True)
    parser.add_argument("--model-config", type=Path, required=True)
    parser.add_argument("--startup-profile", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    profile = {"batch_invariant": False, "startup_sha256": None, "source_sha256": {}}
    if args.startup_profile:
        raw_startup = args.startup_profile.read_bytes()
        startup = json.loads(raw_startup)
        flag = startup["environment"].get("VLLM_BATCH_INVARIANT", "0")
        if flag not in ("0", "1"):
            raise ValueError("Expected explicit batch-invariance flag0 or1")
        profile.update(batch_invariant=flag == "1", startup_sha256=sha(raw_startup))
        if profile["batch_invariant"]:
            profile["source_sha256"] = {
                path: startup["required_source_sha256"][path]
                for path in (
                    "vllm/model_executor/determinism/batch_invariant.py",
                    "vllm/model_executor/determinism/batch_invariant_configs.py",
                    "vllm/platforms/rocm.py",
                )
            }
    source_root = Path(__file__).resolve().parents[2]
    files = {
        name: b"" for name in ("scripts/__init__.py", "scripts/vllm_sparse/__init__.py")
    }
    files["expected-runtime-profile.json"] = json.dumps(
        profile, sort_keys=True, indent=2
    ).encode()
    for relative in (
        "chalkcompute/__init__.py",
        "chalkcompute/weights/__init__.py",
        "chalkcompute/weights/serving/__init__.py",
    ):
        files[relative] = (args.runtime_root / relative).read_bytes()
    for name in ("_weights_format.py", "_weights_codec.py"):
        files["chalkcompute/" + name] = (
            source_root / "chalkcompute" / name
        ).read_bytes()
    for name in ("vllm_worker_ext.py", "vllm_resident_sparse.py"):
        relative = "chalkcompute/weights/serving/" + name
        files[relative] = (args.runtime_root / relative).read_bytes()
    for name in ("runtime_gate.py", "audit_extension.py", "proof.py", "pod_script.py"):
        relative = "scripts/vllm_sparse/" + name
        files[relative] = (source_root / relative).read_bytes()
    overlay_hashes = {name: sha(value) for name, value in files.items()}
    native = {}
    for name in (
        "initial-source-manifest.json",
        "mapping-source-manifest.json",
        "kimi-amd-source-manifest.json",
        "effective-config-source-manifest.json",
    ):
        for entry in json.loads((args.audit_root / name).read_text()):
            if entry["path"].startswith("vllm/") and "sha256" in entry:
                if entry["path"] in native and native[entry["path"]] != entry["sha256"]:
                    raise ValueError("Conflicting native source hashes")
                native[entry["path"]] = entry["sha256"]
    for path, digest in profile["source_sha256"].items():
        if path in native and native[path] != digest:
            raise ValueError("Conflicting batch-invariance native source hash")
        native[path] = digest
    config = args.model_config.read_bytes()
    files["overlay-manifest.json"] = json.dumps(
        overlay_hashes, sort_keys=True, indent=2
    ).encode()
    files["native-source-manifest.json"] = json.dumps(
        native, sort_keys=True, indent=2
    ).encode()
    files["expected-model-config.json"] = config
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressor:
        with tarfile.open(fileobj=compressor, mode="w") as archive:
            for name, data in sorted(files.items()):
                header = tarfile.TarInfo(name)
                header.mode, header.size = 0o644, len(data)
                archive.addfile(header, io.BytesIO(data))
    data = output.getvalue()
    if len(data) > 128 * 1024 or sum(map(len, files.values())) > 512 * 1024:
        raise ValueError("Minimal source overlay exceeds fixed bounds")
    args.out.mkdir()
    (args.out / "overlay.tar.gz").write_bytes(data)
    for name in (
        "overlay-manifest.json",
        "native-source-manifest.json",
        "expected-model-config.json",
    ):
        (args.out / name).write_bytes(files[name])
    plan = {
        "kind": "local_unexecuted_minimal_worker_overlay",
        "resources_claimed": False,
        "image": "vllm/vllm-openai-rocm@sha256:4fcd39b758b660c4e9c345c021e2ea8f0ccdb31e3c32507d676cc3005ae83fba",
        "model": "moonshotai/Kimi-K3",
        "revision": "f831ab66814297da540d832a5235f8e904f29d06",
        "overlay_sha256": sha(data),
        "overlay_bytes": len(data),
        "files": {name: sha(value) for name, value in files.items()},
        "config_sha256": sha(config),
        "selected_architecture_hash": selected_architecture_hash(config),
        "native_source_count": len(native),
        "reviewed_vllm_distribution_versions": REVIEWED_VLLM_VERSIONS,
        "expected_runtime_profile": profile,
        "scope": "experimental minimal worker module overlay, not full installed SDK integration",
        "runtime_source_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=args.runtime_root, text=True
        ).strip(),
        "native_gate": "before any model download: source hashes, packages, Python3.12/vllm0.33, HIP,8gfx950,230GiBfreeeach,tinyindexedkernel",
        "installs": [],
        "missing_dependency_policy": "fail and release, never ad hoc install",
    }
    (args.out / "plan.json").write_text(json.dumps(plan, sort_keys=True, indent=2))
    print(json.dumps(plan, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
