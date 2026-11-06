"""CPU proof helpers shared by the pod script and offline artifact checks."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

NORM = "language_model.model.norm.weight"
HEAD = "language_model.lm_head.weight"
WIDTH = 7168
VOCAB = 163840
TP = 8


def target_indices():
    rows = VOCAB // TP
    return {
        NORM: [0, 1023, 3584, WIDTH - 1],
        HEAD: sorted(
            {
                (rank * rows + row) * WIDTH + col
                for rank in range(TP)
                for row in (0, rows - 1)
                for col in (0, WIDTH - 1)
            }
        ),
    }


def local_indices(name, indices, rank):
    if not 0 <= rank < TP or len(indices) != len(set(indices)):
        raise ValueError("Invalid rank or duplicate global sparse indices")
    if name == NORM:
        if any(type(i) is not int or not 0 <= i < WIDTH for i in indices):
            raise ValueError("Norm index out of bounds")
        return list(indices)
    if name != HEAD:
        raise ValueError("Unsupported exact target")
    first = rank * (VOCAB // TP) * WIDTH
    size = (VOCAB // TP) * WIDTH
    if any(type(i) is not int or not 0 <= i < VOCAB * WIDTH for i in indices):
        raise ValueError("Head index out of bounds")
    return [i - first for i in indices if first <= i < first + size]


def validate_rank_records(records):
    if not isinstance(records, list) or len(records) != TP:
        raise ValueError("Require exactly eight worker records")
    if any(
        type(r["rank"]) is not int or type(r["world_size"]) is not int for r in records
    ):
        raise ValueError("Rank/world identities must be exact integers")
    ranks = {record["rank"] for record in records}
    if ranks != set(range(TP)) or any(r["world_size"] != TP for r in records):
        raise ValueError("Incomplete/distinct rank coverage")
    identities = {(r["pid"], r["process_start_ticks"]) for r in records}
    if len(identities) != TP:
        raise ValueError("Workers must have eight distinct process identities")
    if len({r["device"] for r in records}) != TP:
        raise ValueError("Require eight distinct native logical device ordinals")
    return {r["rank"]: r for r in records}


def assert_stable_workers(before, after):
    old, new = validate_rank_records(before), validate_rank_records(after)
    for rank in old:
        for key in ("pid", "process_start_ticks", "device", "device_uuid", "model_id"):
            if old[rank][key] != new[rank][key]:
                raise ValueError("Native worker identity changed: " + key)
        if old[rank]["tensors"].keys() != new[rank]["tensors"].keys():
            raise ValueError("Inspected tensor set changed")
        for name, tensor in old[rank]["tensors"].items():
            for key in ("storage_ptr", "data_ptr", "shape", "dtype"):
                if tensor[key] != new[rank]["tensors"][name][key]:
                    raise ValueError("Resident tensor identity changed: " + name)


def assert_exact_sparse_bytes(before, after, indices, value_bytes, element_size=2):
    """Independent bytes oracle: change exactly the declared sparse coordinates."""
    original = base64.b64decode(before, validate=True)
    observed = base64.b64decode(after, validate=True)
    if (
        len(original) != len(observed)
        or len(value_bytes) != len(indices) * element_size
    ):
        raise ValueError("Sparse readback byte lengths conflict")
    expected = bytearray(original)
    for n, index in enumerate(indices):
        start = index * element_size
        if type(index) is not int or start < 0 or start + element_size > len(expected):
            raise ValueError("Sparse oracle index out of bounds")
        expected[start : start + element_size] = value_bytes[
            n * element_size : (n + 1) * element_size
        ]
    if bytes(expected) != observed:
        raise ValueError("Readback differs at changed or unchanged tensor bytes")
    return hashlib.sha256(observed).hexdigest()


def selected_architecture_hash(config_bytes):
    """Config + selected schemas only; not a complete trainer architecture hash."""
    from types import SimpleNamespace
    from chalkcompute._weights_format import architecture_hash

    return architecture_hash(
        config_bytes,
        [
            (NORM, SimpleNamespace(shape=(WIDTH,), dtype="bfloat16")),
            (HEAD, SimpleNamespace(shape=(VOCAB, WIDTH), dtype="bfloat16")),
        ],
    )


def write_sparse_version(root: Path, version, parent, patches, architecture_hash):
    """Use actual SDK payload encoding and roundtrip every sparse entry."""
    from chalkcompute._weights_codec import (
        checksum,
        encode_patch,
        read_patch,
        verify_payloads,
    )
    from chalkcompute._weights_format import TensorEntry, WeightManifest

    root.mkdir()
    entries = []
    for i, patch in enumerate(patches):
        if patch.indices is None or not 0 < patch.nnz <= 64:
            raise ValueError("Only tiny actual sparse patches are permitted")
        data = encode_patch(patch)
        name = f"patch-{i}.bin"
        (root / name).write_bytes(data)
        entries.append(
            TensorEntry(
                name=patch.name,
                shape=patch.shape,
                dtype=patch.dtype,
                encoding="sparse",
                file=name,
                offset=0,
                length=len(data),
                nnz=patch.nnz,
                checksum=checksum(data),
            )
        )
    manifest = WeightManifest(
        version,
        parent,
        "patch",
        "moonshotai/Kimi-K3",
        architecture_hash,
        tuple(entries),
    )
    (root / "manifest.json").write_bytes(manifest.to_json())
    verify_payloads(root, manifest)
    for patch, entry in zip(patches, entries, strict=True):
        decoded = read_patch(root, entry)
        if not (
            decoded.indices.equal(patch.indices) and decoded.values.equal(patch.values)
        ):
            raise ValueError("Actual sparse payload roundtrip changed indices/values")
    return manifest
