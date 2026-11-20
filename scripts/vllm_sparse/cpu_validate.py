"""Preserve real CPU codec/byte-oracle fixture evidence without GPU or model loads."""

import argparse
import hashlib
import json
from pathlib import Path

import torch

from chalkcompute._weights_codec import apply_patch, read_patch
from chalkcompute._weights_format import WeightManifest
from scripts.vllm_sparse import pod_script, proof
from scripts.vllm_sparse.audit_extension import tensor_record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir()
    original = {
        proof.NORM: {i: b"\x80\x3f" for i in proof.target_indices()[proof.NORM]},
        proof.HEAD: {i: b"\x00\x3e" for i in proof.target_indices()[proof.HEAD]},
    }
    norm = torch.ones(7168, dtype=torch.bfloat16)
    baseline = tensor_record(norm)["base64"]
    catalog = proof.selected_architecture_hash(
        b'{"model_type":"kimi_k3","cpu_fixture":true}'
    )
    cases = []
    for version, xor in ((1, 1), (2, 3), (3, 0)):
        root = args.out / str(version)
        desired = pod_script.build(root, original, version, version - 1, xor, catalog)
        manifest = WeightManifest.from_json((root / "manifest.json").read_bytes())
        apply_patch(norm, read_patch(root, manifest.tensors[0]))
        proof.assert_exact_sparse_bytes(
            baseline,
            tensor_record(norm)["base64"],
            proof.target_indices()[proof.NORM],
            b"".join(desired[proof.NORM].values()),
        )
        replay_before = norm.clone()
        apply_patch(norm, read_patch(root, manifest.tensors[0]))
        if not norm.equal(replay_before):
            raise ValueError("CPU absolute sparse replay changed state")
        cases.append(
            {
                "version": version,
                "parent": version - 1,
                "xor": xor,
                "manifest_sha256": hashlib.sha256(
                    (root / "manifest.json").read_bytes()
                ).hexdigest(),
                "entries": [
                    {
                        "name": e.name,
                        "global_shape": e.shape,
                        "nnz": e.nnz,
                        "encoded_payload_bytes": e.length,
                        "sparse_value_bytes": e.nnz * 2,
                        "indices": proof.target_indices()[e.name],
                    }
                    for e in manifest.tensors
                ],
                "cpu_norm_changed_and_unchanged_bytes_exact": True,
                "cpu_absolute_replay_exact": True,
            }
        )
    if tensor_record(norm)["base64"] != baseline or torch.cuda.is_initialized():
        raise ValueError("CPU inverse/no-CUDA-init check failed")
    result = {
        "passed": True,
        "scope": "artificial CPU fixture, no native model/runtime execution",
        "torch": torch.__version__,
        "cases": cases,
        "head_elements_materialized": 0,
        "largest_cpu_tensor_elements": 7168,
        "head_local_indices_by_rank": {
            str(r): proof.local_indices(
                proof.HEAD, proof.target_indices()[proof.HEAD], r
            )
            for r in range(8)
        },
        "cuda_initialized": False,
    }
    (args.out / "proof.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
