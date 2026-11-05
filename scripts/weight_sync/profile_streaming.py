"""Synthetic-only streaming fixture assessment; never allocates remote resources."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import resource
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from profile_fixture import AUDIT_NAMES, change_counts, digest, mutate

MAX_DISK_BYTES = 256 * 1024**2


class DiskMeter:
    def __init__(self, root):
        self.root = root
        self.logical = self.allocated = 0

    def sample(self):
        files = [p.stat() for p in self.root.rglob("*") if p.is_file()]
        logical = sum(v.st_size for v in files)
        allocated = sum(v.st_blocks * 512 for v in files)
        self.logical = max(self.logical, logical)
        self.allocated = max(self.allocated, allocated)
        if max(logical, allocated) > MAX_DISK_BYTES:
            raise RuntimeError("Synthetic fixture exceeds256MiB total disk bound")


def file_checksum(path):
    import xxhash

    result = xxhash.xxh3_128()
    with path.open("rb") as reader:
        while block := reader.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def source_catalog(source):
    from safetensors import safe_open

    catalog = {}
    for shard in sorted(source.glob("*.safetensors")):
        with safe_open(shard, framework="pt", device="cpu") as handle:
            for name in handle.keys():
                if name in catalog or handle.get_slice(name).get_dtype() != "BF16":
                    raise ValueError("Require unique canonical BF16 tensors")
                catalog[name] = {
                    "shape": tuple(handle.get_slice(name).get_shape()),
                    "file": shard.name,
                }
    if not catalog:
        raise ValueError("Empty synthetic source")
    return catalog


def generate(source, *, tensors=32, elements=1048576, shard_tensors=4):
    import torch
    from safetensors.torch import save_file

    if (
        not 3 <= tensors <= 1024
        or elements < 100
        or shard_tensors < 1
        # Three dense copies plus conservative patch/header/block-rounding reserve.
        or tensors * elements * 2 * 3 + 32 * 1024**2 > MAX_DISK_BYTES
    ):
        raise ValueError("Synthetic source and all baseline outputs must fit256MiB")
    source.mkdir(parents=True, exist_ok=False)
    names = list(AUDIT_NAMES) + [f"model.layers.{i}.weight" for i in range(tensors - 3)]
    for begin in range(0, tensors, shard_tensors):
        state = {}
        for i, name in enumerate(names[begin : begin + shard_tensors], begin):
            state[name] = torch.randn(
                elements,
                generator=torch.Generator().manual_seed(1701 + i),
                dtype=torch.bfloat16,
            )
        save_file(state, str(source / f"shard-{begin // shard_tensors:03}.safetensors"))
    (source / "config.json").write_text(
        json.dumps(
            {
                "model_type": "synthetic_fixture",
                "tensors": tensors,
                "elements_per_tensor": elements,
            }
        )
    )
    DiskMeter(source.parent).sample()


def encode(source_tensor, target, name, patch_file, patches):
    from chalkcompute._weights_codec import checksum, diff, encode_patch
    from chalkcompute._weights_format import TensorEntry

    patch = diff(source_tensor, target, name=name)
    if patch is None or patch.encoding != "sparse":
        raise RuntimeError("Expected actual sparse BF16 fixture changes")
    blob = encode_patch(patch)
    with patch_file.open("ab") as writer:
        offset = writer.tell()
        writer.write(blob)
    patches.append(
        TensorEntry(
            name,
            "bfloat16",
            tuple(target.shape),
            patch.encoding,
            patch_file.name,
            offset,
            len(blob),
            patch.nnz,
            checksum(blob),
        )
    )


def prove_patch(
    source, patch_root, manifest, counts, trainer_hashes, *, retain_all=False
):
    """Fresh source+persisted patch decode, independently regenerate literal XOR oracle."""
    import torch
    from safetensors.torch import load_file
    from chalkcompute._weights_codec import apply_patch, decode_patch

    entries = {e.name: e for e in manifest.tensors}
    hashes, changed, retained = {}, {}, {}
    for shard in sorted(source.glob("*.safetensors")):
        fresh = load_file(str(shard))
        for name, base in fresh.items():
            entry = entries[name]
            with (patch_root / entry.file).open("rb") as reader:
                reader.seek(entry.offset)
                blob = reader.read(entry.length)
            actual = base.clone()
            apply_patch(actual, decode_patch(entry, blob))
            expected = base.clone()
            bits = expected.view(torch.int16).reshape(-1)
            # Independent scalar oracle: do not call the fixture mutator/diff.
            for i in range(counts[name]):
                index = (i * base.numel()) // counts[name]
                bits[index] = bits[index].bitwise_xor(1)
            if not torch.equal(actual.view(torch.int16), expected.view(torch.int16)):
                raise RuntimeError(
                    "Persisted patch differs from independently regenerated bytes"
                )
            count = int(
                torch.count_nonzero(actual.view(torch.int16) != base.view(torch.int16))
            )
            if count != counts[name] or not torch.isfinite(actual).all():
                raise RuntimeError(
                    "Independent patch finite/changed-count proof failed"
                )
            hashes[name] = digest(actual)
            changed[name] = count
            if hashes[name] != trainer_hashes[name]:
                raise RuntimeError(
                    "Independent patch/trainer tensor byte hashes differ"
                )
            if retain_all:
                retained[name] = actual
    if hashes.keys() != trainer_hashes.keys():
        raise RuntimeError("Patch canonical names differ")
    return hashes, changed, retained


def reference_manifest(reference, catalog, architecture):
    from chalkcompute._weights_format import TensorEntry, WeightManifest

    entries = []
    seen = set()
    for shard in sorted(reference.glob("*.safetensors")):
        checksum = file_checksum(shard)
        from safetensors import safe_open

        with safe_open(shard, framework="pt", device="cpu") as handle:
            for name in handle.keys():
                if (
                    name not in catalog
                    or name in seen
                    or handle.get_slice(name).get_dtype() != "BF16"
                    or tuple(handle.get_slice(name).get_shape())
                    != catalog[name]["shape"]
                ):
                    raise RuntimeError(
                        "Reference mapping/shape differs from canonical source"
                    )
                seen.add(name)
                entries.append(
                    TensorEntry(
                        name,
                        "bfloat16",
                        catalog[name]["shape"],
                        "dense",
                        shard.name,
                        0,
                        shard.stat().st_size,
                        math.prod(catalog[name]["shape"]),
                        checksum,
                    )
                )
    if seen != catalog.keys():
        raise RuntimeError("Reference canonical tensor names differ")
    manifest = WeightManifest(
        0, None, "keyframe", "synthetic/fixture", architecture, tuple(entries)
    )
    (reference / "manifest.json").write_bytes(manifest.to_json())
    return WeightManifest.from_json((reference / "manifest.json").read_bytes())


def prove_reference(reference, expected, manifest):
    from safetensors import safe_open

    hashes = {}
    for file in sorted({e.file for e in manifest.tensors}):
        entries = [e for e in manifest.tensors if e.file == file]
        if file_checksum(reference / file) != entries[0].checksum:
            raise RuntimeError("Reference payload checksum differs")
        with safe_open(reference / file, framework="pt", device="cpu") as handle:
            if set(handle.keys()) != {e.name for e in entries}:
                raise RuntimeError("Reference file/canonical tensor mapping differs")
            for name in handle.keys():
                entry = next(e for e in entries if e.name == name)
                if (
                    tuple(handle.get_slice(name).get_shape()) != tuple(entry.shape)
                    or handle.get_slice(name).get_dtype() != "BF16"
                ):
                    raise RuntimeError("Reference canonical shape/dtype differs")
                hashes[name] = digest(handle.get_tensor(name))
    if hashes != expected:
        raise RuntimeError("Fresh reference/independent patch byte hashes differ")
    return hashes


def build(source, out, mode, budget_root):
    from safetensors.torch import load_file, save_file
    from chalkcompute._weights_format import WeightManifest, architecture_hash

    if mode not in ("retained", "streaming"):
        raise ValueError("Unknown assessment mode")
    out.mkdir(exist_ok=False)
    reference = out / "reference"
    reference.mkdir()
    meter = DiskMeter(budget_root)
    meter.sample()
    catalog = source_catalog(source)
    counts = change_counts({n: v["shape"] for n, v in catalog.items()})
    config = (source / "config.json").read_bytes()
    architecture = architecture_hash(
        config,
        (
            (n, SimpleNamespace(shape=v["shape"], dtype="bfloat16"))
            for n, v in catalog.items()
        ),
    )
    patches = []
    trainer = {}
    patch_file = out / "patches.bin"
    started = time.perf_counter()
    if mode == "retained":
        state = {}
        for shard in sorted(source.glob("*.safetensors")):
            state.update(load_file(str(shard)))
        shadow = {n: t.clone() for n, t in state.items()}
        for name in state:
            state[name] = mutate(state[name], counts[name])
        for name in sorted(state):
            encode(shadow[name], state[name], name, patch_file, patches)
            trainer[name] = digest(state[name])
        save_file(state, str(reference / "model.safetensors"))
        meter.sample()
    else:
        for shard in sorted(source.glob("*.safetensors")):
            state = load_file(str(shard))
            target = {}
            for name, base in state.items():
                target[name] = mutate(base, counts[name])
                encode(base, target[name], name, patch_file, patches)
                trainer[name] = digest(target[name])
            save_file(target, str(reference / shard.name))
            meter.sample()
            del state, target, base
    patch_manifest = WeightManifest(
        1,
        0,
        "patch",
        "synthetic/fixture",
        architecture,
        tuple(sorted(patches, key=lambda e: e.name)),
    )
    (out / "manifest.json").write_bytes(patch_manifest.to_json())
    decoded = WeightManifest.from_json((out / "manifest.json").read_bytes())
    independent, changed, proof_state = prove_patch(
        source, out, decoded, counts, trainer, retain_all=mode == "retained"
    )
    if mode == "retained":
        save_file(proof_state, str(out / "independent-reconstruction.safetensors"))
        meter.sample()
        del state, shadow, proof_state
    del independent
    gc.collect()
    reference_info = reference_manifest(reference, catalog, architecture)
    fresh = prove_reference(reference, trainer, reference_info)
    meter.sample()
    canonical = hashlib.sha256(
        json.dumps(fresh, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {
        "mode": mode,
        "synthetic_only": True,
        "tensor_count": len(catalog),
        "elements": sum(math.prod(v["shape"]) for v in catalog.values()),
        "actual_changed_elements": sum(changed.values()),
        "per_tensor_changed_elements": changed,
        "three_way_hash_catalogs_equal": True,
        "independent_literal_oracle_bytes_equal": True,
        "canonical_tensor_hash_catalog_sha256": canonical,
        "peak_rss_bytes": rss if sys.platform == "darwin" else rss * 1024,
        "peak_logical_disk_bytes": meter.logical,
        "peak_allocated_disk_bytes": meter.allocated,
        "maximum_disk_bytes": MAX_DISK_BYTES,
        "patch_bytes": patch_file.stat().st_size,
        "elapsed_seconds": time.perf_counter() - started,
        "full_8b_projection_measured": False,
        "baseline_scope": "retained target+publisher-shadow+full independent reconstruction shape; not an execution of the network SDK harness",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("--tensors", type=int, default=32)
    parser.add_argument("--elements", type=int, default=1048576)
    parser.add_argument("--mode", choices=("retained", "streaming"))
    args = parser.parse_args()
    import torch

    torch.set_num_threads(2)
    if args.generate:
        generate(args.source, tensors=args.tensors, elements=args.elements)
        return
    if args.out is None or args.mode is None:
        parser.error("build requires out and mode")
    print(
        json.dumps(
            build(args.source, args.out, args.mode, args.source.parent), sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
