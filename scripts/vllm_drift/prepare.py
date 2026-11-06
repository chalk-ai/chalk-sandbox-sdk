"""Offline immutable read-only overlay preparation; never executes a native phase."""

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile

from scripts.vllm_drift.probe import BOUNDS, MODE


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-candidate", type=Path, required=True)
    parser.add_argument("--extra-source-manifest", type=Path, required=True)
    parser.add_argument("--startup-profile", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    prior = json.loads((args.base_candidate / "plan.json").read_bytes())
    raw = (args.base_candidate / "overlay.tar.gz").read_bytes()
    if sha(raw) != prior["overlay_sha256"]:
        raise ValueError("Frozen base archive differs")
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        members = archive.getmembers()
        if (
            len(members) != len(prior["files"])
            or {m.name for m in members} != set(prior["files"])
            or any(not m.isfile() for m in members)
        ):
            raise ValueError("Frozen base archive members differ")
        files = {m.name: archive.extractfile(m).read() for m in members}
    if {p: sha(raw) for p, raw in files.items()} != prior["files"]:
        raise ValueError("Frozen base source digests differ")
    startup_raw = args.startup_profile.read_bytes()
    startup = json.loads(startup_raw)
    profile = json.loads(files["expected-runtime-profile.json"])
    if profile["batch_invariant"] is not False or profile["startup_sha256"] != sha(
        startup_raw
    ):
        raise ValueError("Original frozen startup/batch-off profile required")
    root = Path(__file__).resolve().parents[2]
    for relative in (
        "scripts/vllm_sparse/audit_extension.py",
        "scripts/vllm_sparse/pod_script.py",
        "scripts/vllm_drift/__init__.py",
        "scripts/vllm_drift/probe.py",
        "scripts/vllm_drift/trace.py",
    ):
        files[relative] = (root / relative).read_bytes()
    native = json.loads(files["native-source-manifest.json"])
    for path, digest in json.loads(args.extra_source_manifest.read_bytes()).items():
        if (
            not path.startswith("vllm/")
            or ".." in Path(path).parts
            or len(digest) != 64
        ):
            raise ValueError("Invalid added native source pin")
        if path in native and native[path] != digest:
            raise ValueError("Conflicting native source pin")
        native[path] = digest
    files["native-source-manifest.json"] = json.dumps(
        native, sort_keys=True, indent=2
    ).encode()
    common = [
        "--execute-reviewed",
        "--model-config",
        startup["model_path"] + "/config.json",
        "--expected-config-sha256",
        startup["model_config_sha256"],
    ]
    contract = {
        "mode": MODE,
        "status": "DISABLED_UNEXECUTED_REQUIRES_SEPARATE_CONTROLLER_REVIEW",
        "full_smoke_accepted": False,
        "automatic_fallback": False,
        "startup_sha256": sha(startup_raw),
        "expected_runtime_profile": profile,
        "harness_argv": ["-m", "scripts.vllm_drift.probe", *common],
        "effective_validator_argv": [
            "-m",
            "scripts.vllm_sparse.pod_script",
            *common,
            "--validate-runtime-only",
        ],
        "bounds": BOUNDS,
        "forbidden_mutation_rpc": True,
        "snapshot_requirement": "exact selected norm/head and worker identities before AND after generation; no whole-model immutability claim",
    }
    files["expected-execution-contract.json"] = json.dumps(
        contract, sort_keys=True, indent=2
    ).encode()
    manifest = {
        p: sha(raw)
        for p, raw in files.items()
        if p
        not in (
            "overlay-manifest.json",
            "native-source-manifest.json",
            "expected-model-config.json",
        )
    }
    files["overlay-manifest.json"] = json.dumps(
        manifest, sort_keys=True, indent=2
    ).encode()
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name, raw in sorted(files.items()):
                header = tarfile.TarInfo(name)
                header.size, header.mode = len(raw), 0o644
                archive.addfile(header, io.BytesIO(raw))
    raw = output.getvalue()
    if len(raw) > 128 * 1024 or sum(map(len, files.values())) > 512 * 1024:
        raise ValueError("Minimal overlay bound exceeded")
    plan = {
        **prior,
        "kind": "disabled_read_only_kimi_localization_overlay",
        "resources_claimed": False,
        "mode": MODE,
        "full_smoke_accepted": False,
        "base_plan_sha256": sha((args.base_candidate / "plan.json").read_bytes()),
        "files": {p: sha(raw) for p, raw in files.items()},
        "native_source_count": len(native),
        "execution_contract": contract,
        "execution_contract_sha256": sha(files["expected-execution-contract.json"]),
        "overlay_sha256": sha(raw),
        "overlay_bytes": len(raw),
        "scope": "read-only unchanged numerical localization; no sparse writes or full-smoke acceptance",
    }
    args.out.mkdir()
    (args.out / "overlay.tar.gz").write_bytes(raw)
    for name in (
        "native-source-manifest.json",
        "overlay-manifest.json",
        "expected-execution-contract.json",
        "expected-model-config.json",
    ):
        (args.out / name).write_bytes(files[name])
    (args.out / "plan.json").write_text(json.dumps(plan, sort_keys=True, indent=2))
    print(
        json.dumps(
            {
                "plan_sha256": sha((args.out / "plan.json").read_bytes()),
                "overlay_sha256": plan["overlay_sha256"],
                "file_count": len(files),
                "native_source_count": len(native),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
