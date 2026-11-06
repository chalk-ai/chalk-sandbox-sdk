"""Opt-in direct resident scatter for two audited Kimi-K3 BF16 parameters.

No checkpoint loader is used. Coordinates are explicit resident/global tensor
coordinates, not general checkpoint names. Callers must close admission, drain,
prepare every rank, verify the complete rank set and commit only then. A failed
or uncertain commit requires terminating the entire replica before serving again.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import re
from typing import Any

PROFILE = "kimi-k3-bf16-tp8-v1"
NORM = "language_model.model.norm.weight"
HEAD = "language_model.lm_head.weight"
HIDDEN = 7168
VOCAB = 163840
WORLD = 8
MAX_NNZ = 4096
MAX_PAYLOAD = 256 * 1024


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _token(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch("[0-9a-f]{32}", value) is None:
        raise ValueError("Expected a unique 32-character lowercase hex transaction ID")


def _context(worker: Any) -> tuple[Any, dict[str, Any]]:
    """Verify the exact supported native model/topology; no model-sized reads."""
    from importlib.metadata import version

    import torch

    parallel = importlib.import_module("vllm.distributed.parallel_state")

    if version("vllm") != "0.33.0":
        raise ValueError("Direct resident Kimi scatter requires vLLM 0.33.0")
    model = worker.model_runner.get_model()
    if (
        type(model).__module__ != "vllm.models.kimi_k3.amd.model"
        or type(model).__name__ != "KimiK3ForConditionalGeneration"
        or torch.version.hip is None
        or worker.device.type != "cuda"
        or torch.cuda.get_device_properties(worker.device).gcnArchName.split(":")[0]
        != "gfx950"
        or model.config.model_type != "kimi_k3"
        or model.config.text_config.hidden_size != HIDDEN
        or model.config.text_config.vocab_size != VOCAB
    ):
        raise ValueError("Unsupported native Kimi model/device/config")
    world = parallel.get_world_group()
    tp = parallel.get_tp_group()
    if (
        world.world_size != WORLD
        or tp.world_size != WORLD
        or parallel.get_pp_group().world_size != 1
        or parallel.get_dp_group().world_size != 1
        or world.rank != tp.rank_in_group
    ):
        raise ValueError("Direct resident Kimi scatter requires TP8/PP1/DP1")
    start = Path(f"/proc/{os.getpid()}/stat").read_text().rpartition(")")[2].split()[19]
    identity = {
        "profile": PROFILE,
        "rank": world.rank,
        "tp_rank": tp.rank_in_group,
        "tp_world_size": WORLD,
        "pid": os.getpid(),
        "pid_start_ticks": int(start),
        "device": str(worker.device),
        "model_id": id(model),
    }
    return model, identity


def _targets(model: Any, identity: dict[str, Any]) -> dict[str, Any]:
    import torch

    RMSNorm = importlib.import_module("vllm.model_executor.layers.layernorm").RMSNorm
    embedding = importlib.import_module(
        "vllm.model_executor.layers.vocab_parallel_embedding"
    )
    ParallelLMHead = embedding.ParallelLMHead
    UnquantizedEmbeddingMethod = embedding.UnquantizedEmbeddingMethod

    rank = identity["tp_rank"]
    norm = model.language_model.model.norm
    head = model.language_model.lm_head
    if type(norm) is not RMSNorm or type(head) is not ParallelLMHead:
        raise ValueError("Unsupported resident norm/head module")
    if type(head.quant_method) is not UnquantizedEmbeddingMethod:
        raise ValueError("Resident lm_head must be unquantized")
    shard = head.shard_indices
    rows = VOCAB // WORLD
    begin, end = rank * rows, (rank + 1) * rows
    if (
        head.tp_rank != rank
        or head.tp_size != WORLD
        or head.num_embeddings != VOCAB
        or head.org_vocab_size != VOCAB
        or head.num_added_embeddings != 0
        or head.num_embeddings_padded != VOCAB
        or shard.org_vocab_start_index != begin
        or shard.org_vocab_end_index != end
        or shard.padded_org_vocab_start_index != begin
        or shard.padded_org_vocab_end_index != end
        or shard.num_added_elements != 0
        or shard.num_added_elements_padded != 0
    ):
        raise ValueError("Unsupported lm_head vocabulary shard/padding layout")
    result = {NORM: norm.weight, HEAD: head.weight}
    for name, parameter in result.items():
        shape = (HIDDEN,) if name == NORM else (rows, HIDDEN)
        if (
            type(parameter) is not torch.nn.Parameter
            or tuple(parameter.shape) != shape
            or parameter.dtype != torch.bfloat16
            or parameter.device.type != "cuda"
            or str(parameter.device) != identity["device"]
            or not parameter.is_contiguous()
            or parameter.storage_offset() != 0
            or parameter.untyped_storage().nbytes()
            != parameter.numel() * parameter.element_size()
        ):
            raise ValueError("Unsupported resident parameter shape/dtype/storage")
    if (
        norm.weight.untyped_storage().data_ptr()
        == head.weight.untyped_storage().data_ptr()
    ):
        raise ValueError("Resident parameters must not alias")
    return result


def _snapshot(worker: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    model, identity = _context(worker)
    targets = _targets(model, identity)
    identity["targets"] = {
        name: {
            "shape": list(value.shape),
            "dtype": str(value.dtype),
            "device": str(value.device),
            "data_ptr": value.data_ptr(),
            "storage_ptr": value.untyped_storage().data_ptr(),
            "storage_bytes": value.untyped_storage().nbytes(),
            "parameter_id": id(value),
            "row_start": identity["tp_rank"] * (VOCAB // WORLD) if name == HEAD else 0,
            "row_end": (identity["tp_rank"] + 1) * (VOCAB // WORLD)
            if name == HEAD
            else 1,
        }
        for name, value in targets.items()
    }
    identity["identity_sha256"] = _hash(identity)
    return targets, identity


def local_indices(name: str, indices: Any, rank: int) -> tuple[Any, Any]:
    """Map bounded global flat indices to this rank without expanding a tensor."""
    if type(rank) is not int or not 0 <= rank < WORLD:
        raise ValueError("Invalid TP rank")
    if name == NORM:
        return indices, None
    if name != HEAD:
        raise ValueError("Unsupported resident sparse parameter")
    first = rank * (VOCAB // WORLD) * HIDDEN
    last = (rank + 1) * (VOCAB // WORLD) * HIDDEN
    selected = (indices >= first) & (indices < last)
    return indices[selected] - first, selected


def probe(worker: Any) -> dict[str, Any]:
    _, identity = _snapshot(worker)
    return {**identity, "version": getattr(worker, "_ws_resident_version", 0)}


def prepare(
    worker: Any, directory: str, expected_version: int, transaction_id: str
) -> dict[str, Any]:
    import torch

    from chalkcompute._weights_codec import read_patch, verify_payloads
    from chalkcompute._weights_format import WeightManifest

    _token(transaction_id)
    if type(expected_version) is not int or expected_version < 1:
        raise ValueError("Expected a positive integer target version")
    if getattr(worker, "_ws_resident_failed", False):
        raise RuntimeError("Uncertain resident mutation requires replica restart")
    targets, identity = _snapshot(worker)
    root = Path(directory)
    manifest_path = root / "manifest.json"
    if manifest_path.stat().st_size > MAX_PAYLOAD:
        raise ValueError("Resident sparse manifest exceeds fixed bound")
    with manifest_path.open("rb") as handle:
        raw = handle.read(MAX_PAYLOAD + 1)
    if len(raw) > MAX_PAYLOAD:
        raise ValueError("Resident sparse manifest exceeds fixed bound")
    manifest = WeightManifest.from_json(raw)
    current = getattr(worker, "_ws_resident_version", 0)
    if (
        manifest.kind != "patch"
        or manifest.version != expected_version
        or manifest.parent_version != current
        or manifest.base_model != "moonshotai/Kimi-K3"
        or manifest.architecture_hash
        != getattr(worker, "_ws_resident_architecture", manifest.architecture_hash)
        or not 1 <= len(manifest.tensors) <= 2
        or sum(entry.nnz for entry in manifest.tensors) > MAX_NNZ
    ):
        raise ValueError("Unsupported resident sparse manifest or stale parent/version")
    for entry in manifest.tensors:
        expected_shape = (HIDDEN,) if entry.name == NORM else (VOCAB, HIDDEN)
        if (
            entry.name not in targets
            or entry.encoding != "sparse"
            or entry.dtype != "bfloat16"
            or entry.shape != expected_shape
            or not 0 < entry.nnz <= MAX_NNZ
            or entry.length > MAX_PAYLOAD
        ):
            raise ValueError("Only bounded sparse BF16 norm/head entries are supported")
    if any(
        (root / entry.file).stat().st_size > MAX_PAYLOAD for entry in manifest.tensors
    ):
        raise ValueError("Resident sparse payload exceeds fixed bound")
    verify_payloads(root, manifest)
    manifest_sha256 = hashlib.sha256(raw).hexdigest()
    pending = getattr(worker, "_ws_resident_pending", None)
    if pending is not None:
        if (
            pending["transaction_id"] == transaction_id
            and pending["manifest_sha256"] == manifest_sha256
            and pending["identity_sha256"] == identity["identity_sha256"]
        ):
            return pending["ack"]
        raise RuntimeError("Another resident sparse transaction is already prepared")
    staged = []
    # Complete validation and GPU staging of ALL entries before any resident write.
    for entry in manifest.tensors:
        patch = read_patch(root, entry)
        indices, values = patch.indices, patch.values
        if (
            indices is None
            or indices.dtype != torch.int64
            or indices.ndim != 1
            or values.ndim != 1
            or values.dtype != torch.bfloat16
            or values.numel() != entry.nnz
            or indices.numel() != entry.nnz
            or not torch.isfinite(values).all().item()
            or indices[0].item() < 0
            or indices[-1].item() >= (HIDDEN if entry.name == NORM else VOCAB * HIDDEN)
            or (indices[1:] <= indices[:-1]).any().item()
        ):
            raise ValueError("Invalid sparse values/indices")
        mapped, selected = local_indices(entry.name, indices, identity["tp_rank"])
        local_values = values if selected is None else values[selected]
        parameter = targets[entry.name]
        staged.append(
            (entry.name, mapped.to(parameter.device), local_values.to(parameter.device))
        )
    ack = {
        **identity,
        "ok": True,
        "prepared": True,
        "transaction_id": transaction_id,
        "version": manifest.version,
        "parent_version": current,
        "manifest_sha256": manifest_sha256,
        "architecture_hash": manifest.architecture_hash,
        "local_nnz": {name: indices.numel() for name, indices, _ in staged},
    }
    worker._ws_resident_pending = {
        "transaction_id": transaction_id,
        "manifest_sha256": manifest_sha256,
        "identity_sha256": identity["identity_sha256"],
        "version": manifest.version,
        "parent_version": current,
        "architecture_hash": manifest.architecture_hash,
        "staged": staged,
        "ack": ack,
    }
    return ack


def commit(
    worker: Any,
    transaction_id: str,
    expected_version: int,
    rank_identities: dict[str, str],
    manifest_sha256: str,
) -> dict[str, Any]:
    import torch

    _token(transaction_id)
    if type(expected_version) is not int or expected_version < 1:
        raise ValueError("Expected a positive integer target version")
    if getattr(worker, "_ws_resident_failed", False):
        raise RuntimeError("Uncertain resident mutation requires replica restart")
    targets, identity = _snapshot(worker)
    if (
        not isinstance(rank_identities, dict)
        or set(rank_identities) != {str(rank) for rank in range(WORLD)}
        or len(set(rank_identities.values())) != WORLD
        or rank_identities[str(identity["rank"])] != identity["identity_sha256"]
    ):
        raise ValueError(
            "Commit requires distinct verified identities for all eight ranks"
        )
    previous = getattr(worker, "_ws_resident_last_commit", None)
    if previous is not None and previous["transaction_id"] == transaction_id:
        if (
            previous["version"] != expected_version
            or previous["manifest_sha256"] != manifest_sha256
        ):
            raise ValueError("Committed transaction replay differs")
        return previous
    pending = getattr(worker, "_ws_resident_pending", None)
    if (
        pending is None
        or pending["transaction_id"] != transaction_id
        or pending["version"] != expected_version
        or pending["manifest_sha256"] != manifest_sha256
        or pending["identity_sha256"] != identity["identity_sha256"]
        or pending["parent_version"] != getattr(worker, "_ws_resident_version", 0)
    ):
        raise ValueError("Missing, stale or changed resident preparation")
    # From the first possible write until synchronization any exception is unsafe.
    worker._ws_resident_failed = True
    with torch.no_grad():
        for name, indices, values in pending["staged"]:
            if indices.numel():
                targets[name].view(-1).index_copy_(0, indices, values)
        torch.cuda.synchronize(worker.device)
    worker._ws_resident_version = expected_version
    worker._ws_resident_architecture = pending["architecture_hash"]
    worker._ws_resident_pending = None
    ack = {**pending["ack"], "prepared": False, "committed": True}
    worker._ws_resident_last_commit = ack
    worker._ws_resident_failed = False
    return ack


def abort(worker: Any, transaction_id: str) -> dict[str, Any]:
    _token(transaction_id)
    pending = getattr(worker, "_ws_resident_pending", None)
    if pending is not None and pending["transaction_id"] != transaction_id:
        raise ValueError("Cannot abort a different resident transaction")
    worker._ws_resident_pending = None
    return {"ok": True, "aborted": True, "transaction_id": transaction_id}
