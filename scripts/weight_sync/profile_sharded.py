"""Internal fixture tooling: sharded SDK commits and independent remote byte proof.

One isolated writer owns both existing empty volumes. This is not a publishing API
for concurrent training; it deliberately uses the existing WeightStream commit
checks and refuses every retry after a commit attempt, including lost responses.
"""

from __future__ import annotations

import hashlib
import json
import math
from types import SimpleNamespace

from profile_fixture import change_counts, digest, mutate
from profile_streaming import encode, file_checksum, reference_manifest, source_catalog


def catalog_hash(hashes):
    return hashlib.sha256(
        json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def checkpoint_manifest(folder, base_model):
    from chalkcompute._weights_format import architecture_hash

    catalog = source_catalog(folder)
    config = (folder / "config.json").read_bytes()
    architecture = architecture_hash(
        config,
        (
            (n, SimpleNamespace(shape=v["shape"], dtype="bfloat16"))
            for n, v in catalog.items()
        ),
    )
    manifest = reference_manifest(folder, catalog, architecture)
    from dataclasses import replace

    return replace(manifest, base_model=base_model), catalog


def commit_once(stream, manifest, folder, files, marker):
    """Fence harness replay, delegate commit/retry semantics to existing SDK.

    This marker does not provide backend exactly-once semantics or disable the
    native upload client's internal retries. Ambiguous responses remain failures.
    """
    from chalkcompute._weights_format import safe_path

    if len(files) + 2 > 200 or len(set(files)) != len(files):
        raise ValueError("Require unique files fitting one atomic 200-file commit")
    if any((folder / name).is_symlink() for name in files):
        raise ValueError("Require ordinary immutable fixture payload files")
    paths = [(name, safe_path(folder, name)) for name in files]
    if any(not path.is_file() or path.is_symlink() for _, path in paths):
        raise ValueError("Require ordinary immutable fixture payload files")
    # Marker stays after any exception: remote commit may have succeeded.
    with marker.open("x") as writer:
        writer.write(manifest.to_json().decode())
        writer.flush()
        import os

        os.fsync(writer.fileno())
    stream._commit(manifest, paths)


def read_manifest(snapshot, version):
    from chalkcompute._weights_format import WeightManifest, version_dir

    return WeightManifest.from_json(
        snapshot.read_file(version_dir(version) + "/manifest.json")
    )


def check_mapping(manifest, catalog):
    if {e.name for e in manifest.tensors} != catalog.keys():
        raise RuntimeError("Remote canonical tensor names differ")
    for entry in manifest.tensors:
        if (
            entry.dtype != "bfloat16"
            or tuple(entry.shape) != catalog[entry.name]["shape"]
        ):
            raise RuntimeError("Remote canonical dtype/shape differs")


def verify_keyframe(snapshot, manifest, catalog, folder, expected=None):
    """Download one immutable remote shard at a time, check file mapping and bytes."""
    from safetensors import safe_open
    from chalkcompute._weights_format import version_dir

    check_mapping(manifest, catalog)
    hashes = {}
    for file in sorted({e.file for e in manifest.tensors}):
        entries = [e for e in manifest.tensors if e.file == file]
        path = folder / "remote-shard.safetensors"
        try:
            snapshot.download_to_file(version_dir(manifest.version) + "/" + file, path)
            size = path.stat().st_size
            checksum = file_checksum(path)
            if any(
                e.encoding != "dense"
                or e.offset != 0
                or e.length != size
                or e.checksum != checksum
                for e in entries
            ):
                raise RuntimeError("Remote shard checksum/bounds differ")
            with safe_open(path, framework="pt", device="cpu") as handle:
                if set(handle.keys()) != {e.name for e in entries}:
                    raise RuntimeError("Remote shard canonical file mapping differs")
                for entry in entries:
                    tensor = handle.get_tensor(entry.name)
                    if (
                        tuple(tensor.shape) != tuple(entry.shape)
                        or str(tensor.dtype) != "torch.bfloat16"
                    ):
                        raise RuntimeError("Remote shard canonical shape/dtype differs")
                    hashes[entry.name] = digest(tensor)
                del tensor
        finally:
            path.unlink(missing_ok=True)
    if expected is not None and hashes != expected:
        raise RuntimeError("Remote reference/source tensor bytes differ")
    return hashes


def reconstruct_shard(source_file, manifest, counts, original, trainer, patch_bytes):
    """Persisted remote patch decode, literal oracle, actual finite/count proof."""
    import torch
    from safetensors.torch import load_file
    from chalkcompute._weights_codec import apply_patch, decode_patch

    entries = {e.name: e for e in manifest.tensors}
    targets = {}
    changed = {}
    for name, base in load_file(str(source_file)).items():
        if digest(base) != original[name]:
            raise RuntimeError("Immutable source checkpoint bytes changed")
        entry = entries[name]
        if entry.encoding != "sparse" or entry.nnz != counts[name]:
            raise RuntimeError("Remote patch encoding/count differs")
        blob = patch_bytes[entry.file][entry.offset : entry.offset + entry.length]
        actual = base.clone()
        apply_patch(actual, decode_patch(entry, blob))
        expected = base.clone()
        bits = expected.view(torch.int16).reshape(-1)
        for i in range(counts[name]):
            index = i * base.numel() // counts[name]
            bits[index] = bits[index].bitwise_xor(1)
        if not torch.equal(actual.view(torch.int16), expected.view(torch.int16)):
            raise RuntimeError("Remote patch differs from independent literal bytes")
        count = int(
            torch.count_nonzero(actual.view(torch.int16) != base.view(torch.int16))
        )
        if (
            count != counts[name]
            or not torch.isfinite(actual).all()
            or digest(actual) != trainer[name]
        ):
            raise RuntimeError("Remote patch trainer/finite/changed-count proof failed")
        targets[name], changed[name] = actual, count
    return targets, changed


def prepare(
    stream,
    reference,
    source,
    work,
    *,
    expected_tensors,
    expected_elements,
    sample=lambda stage: None,
):
    """Create an isolated two-version stream and independently sharded reference.

    Source is an already-authorized local checkpoint inside the compute guest.
    Caller owns lifecycle, total fixture disk bound, deadline, and sole writer.
    Both supplied streams must use controller-created, initially empty volumes.
    """
    from safetensors.torch import load_file, save_file
    from chalkcompute._weights_format import WeightManifest, read_pointer, version_dir

    work.mkdir(exist_ok=False)
    if (
        stream.current() is not None
        or reference.current() is not None
        or stream.volume.listdir()
        or reference.volume.listdir()
    ):
        raise RuntimeError("Require both existing isolated empty volumes")
    if any(p.is_symlink() for p in source.iterdir()):
        raise ValueError("Source checkpoint must contain ordinary files")
    baseline, catalog = checkpoint_manifest(source, stream.base_model)
    if (
        len(catalog) != expected_tensors
        or sum(math.prod(v["shape"]) for v in catalog.values()) != expected_elements
    ):
        raise RuntimeError("Source canonical tensor/element count differs")
    shard_files = sorted({e.file for e in baseline.tensors})
    if len(shard_files) + 4 > 200:
        raise ValueError("Source shards/config/index must fit one atomic commit")
    # A canonical HF index makes both sharded keyframes consumable by engines.
    index = {
        "metadata": {"total_size": expected_elements * 2},
        "weight_map": {n: v["file"] for n, v in catalog.items()},
    }
    index_path = source / "model.safetensors.index.json"
    if (
        index_path.exists()
        and json.loads(index_path.read_bytes()).get("weight_map") != index["weight_map"]
    ):
        raise RuntimeError("Source HF index canonical file mapping differs")
    index_path.write_text(json.dumps(index, sort_keys=True))
    auxiliary = sorted(
        p.name
        for p in source.iterdir()
        if p.is_file() and p.name not in {*shard_files, "manifest.json"}
    )
    if len(shard_files) + len(auxiliary) + 2 > 200:
        raise ValueError("All source files must fit one atomic 200-file commit")
    auxiliary_checksums = {file: file_checksum(source / file) for file in auxiliary}
    original = {}
    for file in shard_files:
        for name, tensor in load_file(str(source / file)).items():
            original[name] = digest(tensor)
    del tensor
    sample("baseline_before_upload")
    commit_once(
        stream,
        baseline,
        source,
        shard_files + auxiliary,
        work / "baseline-attempt.json",
    )
    sample("baseline_after_upload")
    base_snapshot = stream._snapshot()
    base_remote = read_manifest(base_snapshot, 0)
    if base_remote != baseline:
        raise RuntimeError("Committed baseline manifest differs")
    verify_keyframe(base_snapshot, base_remote, catalog, work, original)
    sample("baseline_verified")
    counts = change_counts({n: v["shape"] for n, v in catalog.items()})
    if not all(counts.values()):
        raise ValueError("Every fixture tensor must actually change")
    patch_root = work / "patch"
    patch_root.mkdir()
    entries, trainer = [], {}
    for file in shard_files:
        state = load_file(str(source / file))
        for name, tensor in state.items():
            if digest(tensor) != original[name]:
                raise RuntimeError("Immutable source checkpoint bytes changed")
            target = mutate(tensor, counts[name])
            encode(tensor, target, name, patch_root / "patches.bin", entries)
            trainer[name] = digest(target)
        del state, tensor, target
        sample("patch_shard_encoded")
    manifest = WeightManifest(
        1,
        0,
        "patch",
        stream.base_model,
        baseline.architecture_hash,
        tuple(sorted(entries, key=lambda e: e.name)),
        trainer_step=1,
    )
    sample("patch_before_upload")
    commit_once(
        stream, manifest, patch_root, ["patches.bin"], work / "patch-attempt.json"
    )
    sample("patch_after_upload")
    snapshot = stream._snapshot()
    if read_pointer(snapshot.read_file("CURRENT")) != 1:
        raise RuntimeError("Immutable patch snapshot CURRENT differs")
    remote = read_manifest(snapshot, 1)
    if remote != manifest:
        raise RuntimeError("Committed patch manifest differs")
    check_mapping(remote, catalog)
    patch_bytes = {
        file: snapshot.read_file(version_dir(1) + "/" + file)
        for file in {e.file for e in remote.tensors}
    }
    target_root = work / "reference"
    target_root.mkdir()
    import shutil

    for file in auxiliary:
        if file_checksum(source / file) != auxiliary_checksums[file]:
            raise RuntimeError("Immutable source auxiliary bytes changed")
        shutil.copyfile(source / file, target_root / file)
    changed = {}
    for file in shard_files:
        targets, actual = reconstruct_shard(
            source / file, remote, counts, original, trainer, patch_bytes
        )
        save_file(targets, str(target_root / file))
        changed.update(actual)
        del targets
        sample("reference_shard_reconstructed")
    del patch_bytes
    independent, target_catalog = checkpoint_manifest(target_root, stream.base_model)
    if (
        target_catalog != catalog
        or independent.architecture_hash != baseline.architecture_hash
    ):
        raise RuntimeError(
            "Independent reference canonical mapping/architecture differs"
        )
    sample("reference_before_upload")
    commit_once(
        reference,
        independent,
        target_root,
        shard_files + auxiliary,
        work / "reference-attempt.json",
    )
    sample("reference_after_upload")
    ref_snapshot = reference._snapshot()
    ref_manifest = read_manifest(ref_snapshot, 0)
    if (
        ref_manifest != independent
        or read_pointer(ref_snapshot.read_file("CURRENT")) != 0
    ):
        raise RuntimeError("Immutable independent reference manifest/CURRENT differs")
    for file in auxiliary:
        if (
            ref_snapshot.read_file(version_dir(0) + "/" + file)
            != (source / file).read_bytes()
        ):
            raise RuntimeError("Remote reference auxiliary/config/index bytes differ")
    fresh = verify_keyframe(ref_snapshot, ref_manifest, catalog, work, trainer)
    sample("reference_remote_verified")
    if (
        set(changed) != catalog.keys()
        or sum(changed.values()) != expected_elements // 100
    ):
        raise RuntimeError("Independent exact whole-checkpoint changed count differs")
    return {
        "snapshot_id": snapshot.version_id,
        "reference_snapshot_id": ref_snapshot.version_id,
        "tensor_count": len(catalog),
        "elements": expected_elements,
        "actual_changed_elements": sum(changed.values()),
        "per_tensor_changed_elements": changed,
        "canonical_tensor_hash_catalog_sha256": catalog_hash(fresh),
        "whole_model_cpu_bytes_equal": True,
        "independent_literal_oracle_bytes_equal": True,
        "sharded_reference": True,
        "reference_shards": len(shard_files),
        "baseline_manifest_sha256": hashlib.sha256(baseline.to_json()).hexdigest(),
        "patch_manifest_sha256": hashlib.sha256(remote.to_json()).hexdigest(),
        "reference_manifest_sha256": hashlib.sha256(ref_manifest.to_json()).hexdigest(),
        "full_8b_projection_measured": False,
    }
