"""Bounded in-image source/package inspection before any model download or launch.

No installs, downloads, device initialization or resource lifecycle. --inspect-only
collects missing metadata honestly; --check requires an exact reviewed receipt and
CPU codec roundtrip. Run with the same absolute interpreter/PYTHONPATH as vLLM.
"""

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import platform
from pathlib import Path

PACKAGES = ("vllm", "torch", "numpy", "safetensors", "xxhash", "zstandard")


def collect(source_manifest, overlay_manifest):
    versions, errors = {}, []
    for package in PACKAGES:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            errors.append("Missing required installed package:" + package)
    spec = importlib.util.find_spec("vllm")
    if spec is None or not spec.submodule_search_locations:
        raise ValueError("Installed vLLM import root is unavailable")
    package_root = Path(next(iter(spec.submodule_search_locations))).resolve()
    sources = {}
    total = 0
    for relative, expected in source_manifest.items():
        if not relative.startswith("vllm/"):
            raise ValueError("Native source manifest must name vllm package paths")
        path = (package_root / relative.removeprefix("vllm/")).resolve()
        if not path.is_relative_to(package_root) or path.stat().st_size > 512 * 1024:
            raise ValueError("Native source escapes package or exceeds bound")
        data = path.read_bytes()
        total += len(data)
        if total > 4 * 1024**2:
            raise ValueError("Native source read budget exceeded4MiB")
        digest = hashlib.sha256(data).hexdigest()
        sources[relative] = {"sha256": digest, "match": digest == expected}
    root = Path(__file__).resolve().parents[2]
    for relative, expected in overlay_manifest.items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Overlay file escapes frozen root")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Frozen overlay file differs:" + relative)
    chalk = importlib.util.find_spec("chalkcompute")
    if (
        chalk is None
        or Path(chalk.origin).resolve() != root / "chalkcompute/__init__.py"
    ):
        raise ValueError("SDK worker imports do not resolve to exact minimal overlay")
    return {
        "python": platform.python_version(),
        "package_versions": versions,
        "package_root": str(package_root),
        "sources": sources,
        "overlay_files_verified": len(overlay_manifest),
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--overlay-manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--inspect-only", action="store_true")
    choice.add_argument(
        "--check", type=Path, help="Exact reviewed expected package/Python receipt"
    )
    parser.add_argument(
        "--native-device-check",
        action="store_true",
        help="Inside owned image only: eight gfx950 devices, free memory and tiny indexed kernel",
    )
    args = parser.parse_args()
    result = {"passed": False}
    try:
        result.update(
            collect(
                json.loads(args.source_manifest.read_text()),
                json.loads(args.overlay_manifest.read_text()),
            )
        )
        if result["errors"] or not all(
            value["match"] for value in result["sources"].values()
        ):
            raise ValueError("Installed dependencies/source attestation gate failed")
        if result["package_versions"]["vllm"] != "0.33.0" or not result[
            "python"
        ].startswith("3.12."):
            raise ValueError("Pinned vLLM0.33/Python3.12 required")
        if args.check:
            expected = json.loads(args.check.read_text())
            if any(
                result[key] != expected[key] for key in ("python", "package_versions")
            ):
                raise ValueError(
                    "Installed Python/package versions differ from frozen expectations"
                )
        import torch
        from chalkcompute._weights_codec import (
            TensorPatch,
            decode_patch,
            encode_patch,
            checksum,
        )
        from chalkcompute._weights_format import TensorEntry

        if torch.cuda.is_initialized() or not torch.version.hip:
            raise ValueError("Unexpected device initialization or non-HIP Torch build")
        patch = TensorPatch(
            "cpu-gate",
            (7168,),
            "bfloat16",
            torch.tensor([1.25], dtype=torch.bfloat16),
            torch.tensor([1]),
        )
        data = encode_patch(patch)
        entry = TensorEntry(
            name=patch.name,
            dtype=patch.dtype,
            shape=patch.shape,
            encoding="sparse",
            file="gate.bin",
            offset=0,
            length=len(data),
            nnz=1,
            checksum=checksum(data),
        )
        decoded = decode_patch(entry, data)
        if (
            not decoded.values.equal(patch.values)
            or not decoded.indices.equal(patch.indices)
            or torch.cuda.is_initialized()
        ):
            raise ValueError("CPU sparse codec or no-device-init check failed")
        result.update(
            torch_hip=torch.version.hip,
            cpu_sparse_roundtrip=True,
            cuda_initialized_before_native=False,
        )
        if args.native_device_check:
            if torch.cuda.device_count() != 8:
                raise ValueError("Require exactly eight visible native devices")
            devices = []
            for index in range(8):
                props = torch.cuda.get_device_properties(index)
                free, total = torch.cuda.mem_get_info(index)
                if props.gcnArchName.split(":")[0] != "gfx950" or free < 230 * 1024**3:
                    raise ValueError("Require gfx950 with >=230GiB free on each device")
                with torch.cuda.device(index):
                    value = torch.zeros(
                        32, dtype=torch.bfloat16, device=f"cuda:{index}"
                    )
                    value.index_copy_(
                        0,
                        torch.tensor([1, 31], device=value.device),
                        torch.tensor(
                            [1.25, 2.5], dtype=value.dtype, device=value.device
                        ),
                    )
                    torch.cuda.synchronize(index)
                    if value.cpu().tolist() != [0.0, 1.25] + [0.0] * 29 + [2.5]:
                        raise ValueError("Tiny native indexed kernel readback failed")
                    del value
                devices.append(
                    {
                        "index": index,
                        "name": props.name,
                        "gcn_arch": props.gcnArchName,
                        "free_bytes": free,
                        "total_bytes": total,
                        "tiny_sparse_kernel": "passed",
                    }
                )
            result["devices"] = devices
        result["passed"] = True
    except BaseException as error:
        result.update(error_type=type(error).__name__, error=str(error)[:2000])
        raise
    finally:
        args.out.write_text(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
