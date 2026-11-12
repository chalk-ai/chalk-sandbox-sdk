"""Offline archive/hash/import checks using an existing local Python; no installs."""

import argparse
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads((args.candidate / "plan.json").read_text())
    archive = args.candidate / "overlay.tar.gz"
    if hashlib.sha256(archive.read_bytes()).hexdigest() != plan["overlay_sha256"]:
        raise ValueError("Overlay archive digest differs")
    with tempfile.TemporaryDirectory(prefix="minimal-worker-import-proof-") as temp:
        root = Path(temp)
        with tarfile.open(archive) as bundle:
            members = bundle.getmembers()
            if {m.name for m in members} != set(plan["files"]) or len(members) != len(
                plan["files"]
            ):
                raise ValueError("Overlay exact file set differs")
            if sum(m.size for m in members) > 512 * 1024 or any(
                not m.isfile() for m in members
            ):
                raise ValueError("Overlay type/expanded byte bound differs")
            bundle.extractall(root, filter="data")
        for name, digest in plan["files"].items():
            data = (root / name).read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("Exact source digest differs:" + name)
            if name.endswith(".py"):
                compile(data, name, "exec")
        code = """import json,torch,chalkcompute,sys
from chalkcompute.weights.serving import vllm_worker_ext,vllm_resident_sparse
from scripts.vllm_sparse import audit_extension,pod_script
print(json.dumps({"chalkcompute_origin":chalkcompute.__file__,"worker_origin":vllm_worker_ext.__file__,"sparse_origin":vllm_resident_sparse.__file__,"torch_version":torch.__version__,"cuda_initialized":torch.cuda.is_initialized(),"rust_extension_imported":any("_chalk_native" in k for k in sys.modules)}))
"""
        result = subprocess.run(
            [args.python, "-B", "-c", code],
            cwd=root,
            env={**os.environ, "PYTHONPATH": str(root)},
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        observed = json.loads(result.stdout)
        if observed["cuda_initialized"] or observed["rust_extension_imported"]:
            raise ValueError(
                "Minimal import unexpectedly initialized native client/device"
            )
        observed.update(
            overlay_sha256=plan["overlay_sha256"],
            exact_source_files=len(plan["files"]),
            scope="local dependency/import proof only; no native image/model compatibility claim",
        )
        with args.out.open("x") as stream:
            json.dump(observed, stream, indent=2)
        print(json.dumps(observed, indent=2))


if __name__ == "__main__":
    main()
