"""Create a deterministic minimal source overlay locally; no resource calls."""

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile

from scripts.sglang_sparse.driver import ENGINE_KWARGS, MODEL, REVISION, VERSION

IMAGE = "lmsysorg/sglang-rocm@sha256:2456fff6cfaffb822026d089a0629a4859c84d2448864ffd6d5fa4f945ef2c86"
ENV = {
    "SGLANG_USE_ROCM700A": "0",
    "TORCH_BLAS_PREFER_HIPBLASLT": "1",
    "SGLANG_HACK_FLASHMLA_BACKEND": "unified_kv_triton",
    "AITER_BF16_FP8_MOE_BOUND": "0",
    "SGLANG_OPT_USE_AITER_BATCHED_GEMM": "true",
    "SGLANG_OPT_FP8_WO_A_FUSED_INVROPE": "1",
    "SGLANG_HIP_DSV4_C4_INDEXER_ROWGROUP": "1",
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--observed-source",
        type=Path,
        required=True,
        help="Archived actual-source/python/sglang",
    )
    parser.add_argument(
        "--supplemental-source",
        type=Path,
        required=True,
        help="Pinned upstream flattened srt__*.py captures",
    )
    parser.add_argument("--model-config", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2]
    files = {
        name: b""
        for name in (
            "chalkcompute/__init__.py",
            "chalkcompute/weights/__init__.py",
            "chalkcompute/weights/serving/__init__.py",
            "scripts/__init__.py",
            "scripts/sglang_sparse/__init__.py",
        )
    }
    for name in (
        "chalkcompute/_weights_codec.py",
        "chalkcompute/_weights_format.py",
        "chalkcompute/weights/serving/sglang_resident_sparse.py",
        "scripts/sglang_sparse/native.py",
        "scripts/sglang_sparse/driver.py",
    ):
        files[name] = (source / name).read_bytes()
    native, origins = {}, {}
    for name in (
        "srt/entrypoints/engine.py",
        "srt/managers/scheduler.py",
        "srt/managers/tp_worker.py",
        "srt/managers/io_struct.py",
        "srt/managers/tokenizer_manager.py",
        "srt/managers/tokenizer_control_mixin.py",
        "srt/model_executor/model_runner.py",
        "srt/model_executor/model_runner_components/weight_updater.py",
        "srt/runtime_context.py",
        "srt/distributed/parallel_state.py",
        "srt/layers/vocab_parallel_embedding.py",
        "srt/layers/layernorm.py",
        "srt/models/deepseek_v4.py",
        "srt/models/deepseek_common/amd/deepseek_v4_hip.py",
        "srt/server_args.py",
    ):
        path = args.observed_source / name
        origin = "prior observed image source"
        if not path.exists():
            path = args.supplemental_source / name.replace("/", "__")
            origin = "upstream5d8e90 source; native image match required"
        data = path.read_bytes()
        native[name] = sha(data)
        origins[name] = {"path": str(path), "origin": origin}
    files["native-source-manifest.json"] = json.dumps(
        native, sort_keys=True, indent=2
    ).encode()
    files["expected-model-config.json"] = args.model_config.read_bytes()
    profile = {
        "image": IMAGE,
        "distribution": VERSION,
        "actual_upstream_head": "5d8e90a0f96e6ba35603f9b27f2f1039268c810e",
        "engine_kwargs": ENGINE_KWARGS,
        "environment": ENV,
        "model": MODEL,
        "revision": REVISION,
    }
    files["expected-profile.json"] = json.dumps(
        profile, sort_keys=True, indent=2
    ).encode()
    files["overlay-manifest.json"] = json.dumps(
        {name: sha(data) for name, data in files.items()}, sort_keys=True, indent=2
    ).encode()
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressor:
        with tarfile.open(fileobj=compressor, mode="w") as archive:
            for name, data in sorted(files.items()):
                header = tarfile.TarInfo(name)
                header.size, header.mode = len(data), 0o644
                archive.addfile(header, io.BytesIO(data))
    args.out.mkdir()
    (args.out / "overlay.tar.gz").write_bytes(buffer.getvalue())
    for name in (
        "native-source-manifest.json",
        "expected-profile.json",
        "expected-model-config.json",
        "overlay-manifest.json",
    ):
        (args.out / name).write_bytes(files[name])
    plan = {
        "status": "CANDIDATE_DISABLED_REQUIRES_ROOT_FREEZE",
        "resources_claimed": False,
        "profile": profile,
        "native_source_origins": origins,
        "files": {name: sha(data) for name, data in files.items()},
        "overlay_sha256": sha(buffer.getvalue()),
        "native_source_count": len(native),
        "config_sha256": sha(files["expected-model-config.json"]),
        "minimum_native_work_seconds": 900,
        "startup_timeout_seconds": 600,
        "full_smoke_accepted": False,
        "scope": "offline native Engine real BF16 resident indexed writes; quantized experts unsupported",
    }
    (args.out / "plan.json").write_text(json.dumps(plan, sort_keys=True, indent=2))
    print(
        json.dumps(
            {
                "plan_sha256": sha((args.out / "plan.json").read_bytes()),
                "tar_sha256": plan["overlay_sha256"],
                "files": len(files),
                "native": len(native),
            }
        )
    )


if __name__ == "__main__":
    main()
