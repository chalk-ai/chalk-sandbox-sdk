"""Opt-in scheduler extension for the frozen offline Engine proof only."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def audit(worker):
    import torch
    from chalkcompute.weights.serving import sglang_resident_sparse as sparse

    targets, identity = sparse._snapshot(worker)
    result = {}
    for name, parameter in targets.items():
        # Explicit full-target test oracle. This cost is not production delta work.
        value = parameter.detach().flatten().to(device="cpu", copy=True)
        indices = [0, value.numel() - 1]
        raw = value.view(torch.uint8).numpy()
        digest = hashlib.sha256(memoryview(raw)).hexdigest()
        selected = value[indices].view(torch.uint8).numpy().tobytes().hex()
        value[indices] = 0
        result[name] = {
            "sha256": digest,
            "untouched_sha256": hashlib.sha256(memoryview(raw)).hexdigest(),
            "local_indices": indices,
            "selected_hex": selected,
        }
    return {
        **identity,
        "version": getattr(worker, "_ws_resident_version", 0),
        "bytes": result,
    }


def _operation(scheduler, operation, payload):
    import torch
    from torch.utils._python_dispatch import TorchDispatchMode
    from chalkcompute.weights.serving import sglang_resident_sparse as sparse
    from sglang.srt.managers.io_struct import (
        PauseGenerationReqInput,
        ContinueGenerationReqInput,
    )

    worker = scheduler.tp_worker
    if scheduler.draft_worker is not None or scheduler.enable_overlap:
        raise ValueError("Proof requires no speculation and overlap disabled")
    if not scheduler.is_fully_idle():
        raise RuntimeError("Native scheduler has outstanding requests")
    if operation == "pause":
        scheduler.pause_generation(PauseGenerationReqInput(mode="in_place"))
        torch.cuda.synchronize(worker.gpu_id)
        return {
            **sparse.probe(worker),
            "paused": scheduler._engine_paused,
            "idle": True,
        }
    if not scheduler._engine_paused:
        raise RuntimeError("Resident proof requires paused native scheduler")
    if operation == "resume":
        if not scheduler.flush_cache(empty_cache=False):
            raise RuntimeError("Native cache flush refused")
        scheduler.continue_generation(ContinueGenerationReqInput())
        if scheduler._engine_paused:
            raise RuntimeError("Native resume did not take effect")
        return {**sparse.probe(worker), "paused": False, "cache_flushed": True}
    if operation == "snapshot":
        return audit(worker)
    if operation == "probe":
        # Fail if an update accidentally enters a full loader after bootstrap.
        def forbidden(*args, **kwargs):
            raise RuntimeError("Full weight loader forbidden after native bootstrap")

        worker.model_runner.model.load_weights = forbidden
        return sparse.probe(worker)
    if operation == "prepare":
        return sparse.prepare(worker, **payload)
    if operation == "abort":
        return sparse.abort(worker, **payload)
    if operation == "commit":
        writes = []

        class Guard(TorchDispatchMode):
            def __torch_dispatch__(self, func, types, args=(), kwargs=None):
                if func._schema.is_mutable:
                    if str(func) != "aten.index_copy_.default":
                        raise RuntimeError(f"Forbidden resident write {func}")
                    writes.append(
                        {
                            "op": str(func),
                            "nnz": args[2].numel(),
                            "data_ptr": args[0].data_ptr(),
                        }
                    )
                return func(*args, **(kwargs or {}))

        with Guard():
            result = sparse.commit(worker, **payload)
        return {**result, "writes": writes}
    raise ValueError("Unsupported resident proof operation")


def scheduler_rpc(self, call_id: str, operation: str, payload_json: str):
    from chalkcompute.weights.serving import sglang_resident_sparse as sparse
    from sglang.srt.runtime_context import get_parallel

    sparse._token(call_id)
    rank = get_parallel().tp_rank
    root = Path(os.environ["AMD_EXPERIMENT_RUN_DIR"]) / "receipts"
    root.mkdir(exist_ok=True)
    envelope = {
        "schema_version": 1,
        "call_id": call_id,
        "method": operation,
        "rank": rank,
    }
    try:
        if len(payload_json) > 65536:
            raise ValueError("RPC payload too large")
        result = _operation(self, operation, json.loads(payload_json))
        envelope.update(ok=True, result=result)
    except Exception as error:
        # Keep all original ranks in the pinned RPC barrier. Host requires all
        # eight explicit envelopes; aggregate RPC success alone proves nothing.
        envelope.update(ok=False, error_type=type(error).__name__, error=str(error))
    data = json.dumps(envelope, sort_keys=True, allow_nan=False).encode()
    temporary = root / f"{call_id}.{rank}.tmp"
    with temporary.open("xb") as handle:
        handle.write(data)
    temporary.rename(root / f"{call_id}.{rank}.json")


def run_scheduler_process(*args, **kwargs):
    from sglang.srt.managers.scheduler import Scheduler, run_scheduler_process as native

    if hasattr(Scheduler, "ws_sparse_rpc"):
        raise RuntimeError("Resident proof extension already installed")
    Scheduler.ws_sparse_rpc = scheduler_rpc
    return native(*args, **kwargs)
