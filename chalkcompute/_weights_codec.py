"""Lossless absolute tensor patches. Torch and compression are optional extras."""

from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from chalkcompute._weights_format import (
    TensorEntry,
    WeightManifest,
    WeightStreamError,
    safe_path,
)

if TYPE_CHECKING:
    import torch


@dataclass(frozen=True)
class TensorPatch:
    name: str
    shape: tuple[int, ...]
    dtype: str
    values: torch.Tensor
    indices: torch.Tensor | None = None

    @property
    def encoding(self) -> Literal["dense", "sparse"]:
        return "dense" if self.indices is None else "sparse"

    @property
    def nnz(self) -> int:
        return self.values.numel()


def _torch():
    try:
        import torch

        return torch
    except ImportError as exc:
        raise ImportError(
            "Install chalkcompute[weights] to publish or decode tensor patches"
        ) from exc


def checksum(payload: bytes) -> str:
    import xxhash

    return xxhash.xxh3_128_hexdigest(payload)


def diff(
    prev: torch.Tensor,
    new: torch.Tensor,
    *,
    name: str = "",
    density_threshold: float = 0.3,
) -> TensorPatch | None:
    torch = _torch()
    if not 0 <= density_threshold <= 1:
        raise ValueError("density_threshold must be between zero and one")
    if prev.shape != new.shape or prev.dtype != new.dtype or prev.device != new.device:
        raise WeightStreamError(
            "Tensor shape, dtype and device must match the previous publication"
        )
    old = prev.detach().contiguous().reshape(-1)
    values = new.detach().contiguous().reshape(-1)
    # Compare storage bits, preserving signed zero and NaN payload changes.
    changed = (
        old.view(torch.uint8).reshape(old.numel(), old.element_size())
        != values.view(torch.uint8).reshape(values.numel(), values.element_size())
    ).any(dim=1)
    indices = torch.nonzero(changed).reshape(-1)
    if not indices.numel():
        return None
    shape, dtype = tuple(new.shape), str(new.dtype).removeprefix("torch.")
    if indices.numel() / max(new.numel(), 1) > density_threshold:
        return TensorPatch(name, shape, dtype, values.clone())
    return TensorPatch(name, shape, dtype, values[indices].clone(), indices)


def encode_patch(patch: TensorPatch) -> bytes:
    import zstandard

    torch = _torch()
    values = patch.values.detach().contiguous().cpu().reshape(-1)
    raw_values = values.view(torch.uint8).numpy().tobytes()
    raw_indices = b""
    index_dtype = None
    if patch.indices is not None:
        indices = patch.indices.detach().to(device="cpu", dtype=torch.int64).reshape(-1)
        if indices.numel() and (
            indices[0] < 0
            or indices[-1] >= math.prod(patch.shape)
            or (indices[1:] <= indices[:-1]).any()
        ):
            raise WeightStreamError(
                "Sparse indices must be sorted, unique and in bounds"
            )
        if indices.numel() != values.numel():
            raise WeightStreamError("Sparse indices and values lengths differ")
        deltas = indices.clone()
        deltas[1:] = indices[1:] - indices[:-1]
        index_dtype = "int32" if math.prod(patch.shape) <= 2**31 - 1 else "int64"
        raw_indices = deltas.to(getattr(torch, index_dtype)).numpy().tobytes()
    elif values.numel() != math.prod(patch.shape):
        raise WeightStreamError("Dense patch size does not match shape")
    header = json.dumps(
        {
            "schema_version": 1,
            "index_dtype": index_dtype,
            "values_bytes": len(raw_values),
        },
        sort_keys=True,
    ).encode()
    return zstandard.ZstdCompressor(level=3).compress(
        struct.pack("<I", len(header)) + header + raw_values + raw_indices
    )


def decode_patch(entry: TensorEntry, payload: bytes) -> TensorPatch:
    import zstandard

    torch = _torch()
    if len(payload) != entry.length or checksum(payload) != entry.checksum:
        raise WeightStreamError(f"Checksum/length mismatch for {entry.name}")
    dtype = getattr(torch, entry.dtype, None)
    if dtype is None or not isinstance(dtype, torch.dtype):
        raise WeightStreamError(f"Unsupported tensor dtype: {entry.dtype}")
    size = torch.empty((), dtype=dtype).element_size()
    # Bound decompression by declared element counts, including metadata reserve.
    limit = 4096 + entry.nnz * (size + (8 if entry.encoding == "sparse" else 0))
    try:
        if zstandard.frame_content_size(payload) > limit:
            raise ValueError("Decompressed patch exceeds manifest bounds")
        raw = zstandard.ZstdDecompressor().decompress(payload, max_output_size=limit)
        header_len = struct.unpack("<I", raw[:4])[0]
        if header_len > 4096:
            raise ValueError("Patch header too large")
        header = json.loads(raw[4 : 4 + header_len])
        if header["schema_version"] != 1 or header["values_bytes"] != entry.nnz * size:
            raise ValueError("Patch header does not match manifest")
        body = raw[4 + header_len :]
        nbytes = entry.nnz * size
        if len(body) < nbytes:
            raise ValueError("Truncated patch values")
        values = (
            torch.frombuffer(bytearray(body[:nbytes]), dtype=dtype).clone()
            if nbytes
            else torch.empty(0, dtype=dtype)
        )
        indices = None
        if entry.encoding == "sparse":
            if header["index_dtype"] not in ("int32", "int64"):
                raise ValueError("Invalid index dtype")
            index_dtype = getattr(torch, header["index_dtype"])
            if (
                len(body[nbytes:])
                != entry.nnz * torch.empty((), dtype=index_dtype).element_size()
            ):
                raise ValueError("Invalid sparse index length")
            deltas = (
                torch.frombuffer(bytearray(body[nbytes:]), dtype=index_dtype).to(
                    torch.int64
                )
                if entry.nnz
                else torch.empty(0, dtype=torch.int64)
            )
            if deltas.numel() and (deltas[0] < 0 or (deltas[1:] <= 0).any()):
                raise ValueError("Invalid sparse index deltas")
            indices = deltas.cumsum(0)
            if indices.numel() and indices[-1] >= math.prod(entry.shape):
                raise ValueError("Sparse index outside tensor")
        elif header["index_dtype"] is not None or len(body) != nbytes:
            raise ValueError("Invalid dense payload")
        return TensorPatch(entry.name, entry.shape, entry.dtype, values, indices)
    except (
        ValueError,
        KeyError,
        struct.error,
        RuntimeError,
        zstandard.ZstdError,
    ) as exc:
        raise WeightStreamError(
            f"Invalid tensor payload for {entry.name}: {exc}"
        ) from exc


def apply_patch(tensor: torch.Tensor, patch: TensorPatch) -> None:
    torch = _torch()
    if (
        tuple(tensor.shape) != patch.shape
        or str(tensor.dtype).removeprefix("torch.") != patch.dtype
        or not tensor.is_contiguous()
    ):
        raise WeightStreamError("Patch target shape, dtype or layout mismatch")
    with torch.no_grad():
        values = patch.values.to(tensor.device)
        if patch.indices is None:
            tensor.copy_(values.reshape(patch.shape))
        else:
            tensor.reshape(-1).index_copy_(0, patch.indices.to(tensor.device), values)


def payload_bytes(root: Path, entry: TensorEntry) -> bytes:
    path = safe_path(root, entry.file)
    with path.open("rb") as stream:
        stream.seek(entry.offset)
        payload = stream.read(entry.length)
    if len(payload) != entry.length or checksum(payload) != entry.checksum:
        raise WeightStreamError(f"Checksum/length mismatch for {entry.name}")
    return payload


def read_patch(root: Path, entry: TensorEntry) -> TensorPatch:
    return decode_patch(entry, payload_bytes(root, entry))


def verify_payloads(root: Path, manifest: WeightManifest) -> None:
    """Verify every payload before the caller begins any GPU mutation."""
    seen = set()
    for entry in manifest.tensors:
        identity = (entry.file, entry.offset, entry.length, entry.checksum)
        if identity not in seen:
            import xxhash

            digest = xxhash.xxh3_128()
            remaining = entry.length
            with safe_path(root, entry.file).open("rb") as stream:
                stream.seek(entry.offset)
                while remaining:
                    chunk = stream.read(min(4 * 1024 * 1024, remaining))
                    if not chunk:
                        raise WeightStreamError(f"Truncated payload for {entry.name}")
                    digest.update(chunk)
                    remaining -= len(chunk)
            if digest.hexdigest() != entry.checksum:
                raise WeightStreamError(f"Checksum mismatch for {entry.name}")
            seen.add(identity)
