"""Remote CPU-only preparation of an exact 1% stream and independent reference."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

from profile_fixture import (
    AUDIT_NAMES,
    change_counts,
    digest,
    mutate,
    require_target_changed,
    sparse_memory_estimate,
)


def prepare_fixture(stream, hf_folder, work):
    return _prepare_fixture(stream, hf_folder, work, 399, 8190735360)


def _prepare_fixture(stream, hf_folder, work, expected_tensors, expected_elements):
    """Caller independently proves the pinned HF seed before entering here."""
    import torch
    import xxhash
    import chalkcompute as cc
    from safetensors.torch import load_file
    from chalkcompute._weights_format import WeightManifest, pointer_json, version_dir

    work = Path(work)
    work.mkdir(parents=True, exist_ok=False)
    state = {}
    for shard in sorted(Path(hf_folder).glob("*.safetensors")):
        values = load_file(str(shard))
        if state.keys() & values.keys():
            raise ValueError("Duplicate HF tensor name")
        state.update(values)
    del values
    if (
        len(state) != expected_tensors
        or sum(t.numel() for t in state.values()) != expected_elements
    ):
        raise ValueError("Require the exact reviewed Qwen3-8B canonical model")
    counts = change_counts({name: tuple(t.shape) for name, t in state.items()})
    if not all(counts.values()):
        raise ValueError("Every canonical tensor must receive actual changes")
    base_hashes = {name: digest(state[name]) for name in AUDIT_NAMES}
    publisher = stream.publisher(keyframe_every=50)
    publisher.resume(state)
    # Build one target tensor at a time; no additional retained full-model clone.
    for name in state:
        state[name] = mutate(state[name], counts[name])
    target_hashes = {name: digest(state[name]) for name in AUDIT_NAMES}
    require_target_changed(base_hashes, target_hashes)
    if publisher.publish(state, step=1) != 1:
        raise RuntimeError("Unexpected fixture publication version")
    snapshot = stream._snapshot()
    manifest = WeightManifest.from_json(
        snapshot.read_file(version_dir(1) + "/manifest.json")
    )
    memory = {
        **sparse_memory_estimate(manifest.tensors),
        "cpu_memory_limit_bytes": 128 * 1024**3,
        "cpu_conservative_peak_bytes": 112 * 1024**3,
        "cpu_peak_basis": "up to six16.4GB model/file buffers plus largest-tensor decode/index/serialization workspace; reference reread occurs after state/publisher release",
        "guest_disk_conservative_peak_bytes": 80 * 1024**3,
        "host_free_disk_required_bytes": 120 * 1024**3,
    }
    materialized = stream.materialize(1, work / "independent-target")
    expected = load_file(str(materialized / "model.safetensors"))
    if expected.keys() != state.keys():
        raise RuntimeError("Independent reconstruction changed canonical names")
    canonical = hashlib.sha256()
    for name in sorted(state):
        actual = state[name].contiguous().view(torch.int16)
        if not torch.equal(actual, expected[name].contiguous().view(torch.int16)):
            raise RuntimeError("Independent full-model reconstruction differs: " + name)
        canonical.update(name.encode())
        canonical.update(bytes.fromhex(digest(expected[name])))
    del expected, actual
    tensor_count = len(state)
    elements = sum(t.numel() for t in state.values())
    # The independent reference is now the oracle. Drop both retained model
    # states before rereading its16.4GB remote object for snapshot verification.
    del state, publisher
    import gc

    gc.collect()
    reference = cc.Volume(stream.name + "-reference")
    reference.reload()
    if reference.listdir():
        raise RuntimeError("Reference volume is not empty")
    original = WeightManifest.from_json(
        snapshot.read_file(version_dir(0) + "/manifest.json")
    )
    payload = materialized / "model.safetensors"
    checksum = xxhash.xxh3_128()
    with payload.open("rb") as reader:
        while block := reader.read(4 * 1024 * 1024):
            checksum.update(block)
    entries = tuple(
        replace(
            e,
            file=payload.name,
            offset=0,
            length=payload.stat().st_size,
            checksum=checksum.hexdigest(),
        )
        for e in original.tensors
    )
    independent = replace(original, tensors=entries)
    with reference.batch_upload(max_files_per_commit=200) as batch:
        for file in sorted(materialized.iterdir()):
            if file.is_file() and file.name not in {
                "manifest.json",
                "chalk-weight-version.json",
            }:
                batch.put_file(version_dir(0) + "/" + file.name, file)
        batch.put(version_dir(0) + "/manifest.json", independent.to_json())
        batch.put("CURRENT", pointer_json(0))
    stream.pin(0)
    stream_snapshot = stream._snapshot()
    reference.reload()
    reference_snapshot = reference.at_version(reference.version.id)
    # Reread reference payload independently from its immutable remote snapshot.
    # One file at a time, never a model download to the workstation.
    remote = reference_snapshot.read_file(version_dir(0) + "/" + payload.name)
    if (
        len(remote) != payload.stat().st_size
        or xxhash.xxh3_128_hexdigest(remote) != checksum.hexdigest()
    ):
        raise RuntimeError("Reference snapshot bytes differ from reconstruction")
    del remote
    return {
        "volume_name": stream.name,
        "reference_volume_name": stream.name + "-reference",
        "snapshot_id": stream_snapshot.version_id,
        "reference_snapshot_id": reference.version.id,
        "current": 1,
        "pin": 0,
        "reference_current": 0,
        "tensor_count": tensor_count,
        "elements": elements,
        "actual_changed_elements": sum(counts.values()),
        "per_tensor_changed_elements": counts,
        "base_three_parameter_hashes": base_hashes,
        "target_three_parameter_hashes": target_hashes,
        "native_audit_scope": "three independently verified unfused parameters; not whole native model",
        "whole_model_cpu_bytes_equal": True,
        "canonical_tensor_hash_catalog_sha256": canonical.hexdigest(),
        "manifests_sha256": {
            str(v): hashlib.sha256(
                stream_snapshot.read_file(version_dir(v) + "/manifest.json")
            ).hexdigest()
            for v in (0, 1)
        },
        "reference_manifest_sha256": hashlib.sha256(
            reference_snapshot.read_file(version_dir(0) + "/manifest.json")
        ).hexdigest(),
        "payload_bytes": sum(e.length for e in manifest.tensors),
        "transmitted_elements": sum(e.nnz for e in manifest.tensors),
        "memory_estimate": memory,
    }
