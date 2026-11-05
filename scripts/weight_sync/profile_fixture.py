"""Deterministic finite BF16 fixture and native digest verifier; no allocations."""

from __future__ import annotations

import hashlib
import math

AUDIT_NAMES = (
    "model.embed_tokens.weight",
    "model.norm.weight",
    "lm_head.weight",
)


def change_counts(shapes):
    """Largest-remainder allocation: exactly floor(total/100) across the model."""
    sizes = {name: math.prod(shape) for name, shape in sorted(shapes.items())}
    if not sizes or any(size <= 0 for size in sizes.values()):
        raise ValueError("Require nonempty positive tensor shapes")
    target = sum(sizes.values()) // 100
    counts = {name: size // 100 for name, size in sizes.items()}
    left = target - sum(counts.values())
    for name in sorted(sizes, key=lambda n: (-(sizes[n] % 100), n))[:left]:
        counts[name] += 1
    assert sum(counts.values()) == target
    return counts


def mutate(tensor, count):
    """Flip only BF16 mantissa bit zero; no exponent/sign change is possible."""
    import torch

    if tensor.dtype != torch.bfloat16 or tensor.device.type != "cpu":
        raise ValueError("Fixture requires CPU BF16 tensors")
    if type(count) is not int or not 0 <= count <= tensor.numel():
        raise ValueError("Invalid exact fixture count")
    if not torch.isfinite(tensor).all():
        raise ValueError("Source tensor contains nonfinite values")
    result = tensor.detach().contiguous().clone()
    if count:
        indices = torch.arange(count, dtype=torch.int64) * tensor.numel() // count
        bits = result.view(torch.int16).reshape(-1)
        bits[indices] = bits[indices].bitwise_xor(1)
    if not torch.isfinite(result).all():
        raise ValueError("Target tensor contains nonfinite values")
    changed = int(
        torch.count_nonzero(
            result.reshape(-1).view(torch.int16)
            != tensor.contiguous().reshape(-1).view(torch.int16)
        )
    )
    if changed != count:
        raise RuntimeError("Actual fixture changes differ from allocation")
    return result


def digest(tensor):
    import torch

    view = memoryview(
        tensor.detach().cpu().contiguous().reshape(-1).view(torch.uint8).numpy()
    ).cast("B")
    result = hashlib.sha256()
    for start in range(0, len(view), 8 * 1024 * 1024):
        result.update(view[start : start + 8 * 1024 * 1024])
    return result.hexdigest()


def verify_native(result, expected, *, workers=1):
    """Exact unfused names are accepted only after native/CPU byte agreement."""
    ranks = result.get("results") if isinstance(result, dict) else None
    if not isinstance(ranks, list) or len(ranks) != workers or not expected:
        raise ValueError("Missing native audit acknowledgements")
    if any(not isinstance(rank, dict) or rank != expected for rank in ranks):
        raise ValueError("Native parameter mapping/digests differ from CPU reference")
    return True


def require_target_changed(base, target):
    if set(base) != set(AUDIT_NAMES) or set(target) != set(AUDIT_NAMES):
        raise ValueError("Require the exact reviewed native audit names")
    if any(base[name] == target[name] for name in AUDIT_NAMES):
        raise ValueError("Every audited native parameter must actually change")


def sparse_memory_estimate(entries, *, batch_bytes=256 * 1024**2):
    """Mirror existing worker batching, including full native NaN expansion."""
    batches, current, staged = [], [], 0
    largest = max((math.prod(e.shape) * 2 for e in entries), default=0)
    for entry in entries:
        if entry.encoding == "dense":
            if current:
                batches.append(current)
            current, staged = [], 0
            continue
        size = entry.nnz * 10
        if staged + size > batch_bytes and current:
            batches.append(current)
            current, staged = [], 0
        current.append(entry)
        staged += size
    if current:
        batches.append(current)
    staged = max((sum(e.nnz * 10 for e in b) for b in batches), default=0)
    expanded = max((sum(math.prod(e.shape) * 2 for e in b) for b in batches), default=0)
    reserve = 8 * 1024**3
    required = 40 * 1024**3 + 2 * 1024**3 + 2 * largest + expanded + staged + reserve
    return {
        "maximum_sparse_staged_bytes": staged,
        "maximum_sparse_expanded_nan_bytes": expanded,
        "largest_tensor_bytes": largest,
        "sparse_batches": len(batches),
        "minimum_free_reserve_bytes": reserve,
        "assumed_engine_reservation_bytes": 40 * 1024**3,
        "additional_workspace_bytes": 2 * 1024**3,
        "estimated_gpu_bytes_including_reserve": required,
        "fits_80gib_with_reserve": required <= 80 * 1024**3,
        "estimate_is_not_a_measured_peak": True,
    }


def check_workspace(root, minimum_bytes):
    """Verify guest capacity and actual fallocate/quota before HF or uploads."""
    import os

    before = os.statvfs(root)
    free = before.f_bavail * before.f_frsize
    if free < minimum_bytes:
        raise RuntimeError("Guest workspace has insufficient available bytes")
    path = root / "workspace-quota-probe"
    try:
        with path.open("xb") as handle:
            # Unlike a sparse truncate, fallocate exercises filesystem/quota
            # capacity. Unsupported/EDQUOT/ENOSPC is a hard pre-upload failure.
            allocate = getattr(os, "posix_fallocate", None)
            if allocate is None:
                raise RuntimeError("Guest workspace fallocate is unsupported")
            allocate(handle.fileno(), 0, minimum_bytes)
            allocated = os.fstat(handle.fileno()).st_blocks * 512
            if allocated < minimum_bytes:
                raise RuntimeError("Guest workspace reservation is sparse/incomplete")
    finally:
        path.unlink(missing_ok=True)
    after = os.statvfs(root)
    remaining = after.f_bavail * after.f_frsize
    if remaining < minimum_bytes:
        raise RuntimeError("Guest workspace reservation release unconfirmed")
    return {
        "path": str(root),
        "filesystem_total_bytes": before.f_blocks * before.f_frsize,
        "available_bytes_before": free,
        "available_bytes_after": remaining,
        "quota_reservation_bytes_verified": allocated,
        "minimum_required_bytes": minimum_bytes,
        "quota_reservation_released": True,
    }
