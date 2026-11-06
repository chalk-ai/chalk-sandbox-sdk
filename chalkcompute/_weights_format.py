"""Versioned, engine-independent weight stream metadata (no torch dependency)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal


class WeightStreamError(Exception):
    """Invalid or inconsistent weight stream data."""


def version_dir(version: int) -> str:
    if type(version) is not int or version < 0:
        raise WeightStreamError("Weight versions must be nonnegative integers")
    return f"v{version:06d}"


def safe_path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if not relative or path.is_absolute() or ".." in path.parts or "\\" in relative:
        raise WeightStreamError(f"Unsafe weight payload path: {relative!r}")
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise WeightStreamError(
            f"Weight payload escapes version directory: {relative!r}"
        )
    return resolved


@dataclass(frozen=True)
class TensorEntry:
    name: str
    dtype: str
    shape: tuple[int, ...]
    encoding: Literal["sparse", "dense"]
    file: str
    offset: int
    length: int
    nnz: int
    checksum: str

    def __post_init__(self) -> None:
        if not self.name or self.encoding not in ("sparse", "dense"):
            raise WeightStreamError("Invalid tensor name or encoding")
        if any(type(n) is not int or n < 0 for n in self.shape):
            raise WeightStreamError("Invalid tensor shape")
        if any(
            type(n) is not int or n < 0 for n in (self.offset, self.length, self.nnz)
        ):
            raise WeightStreamError("Invalid payload bounds")
        if self.nnz > math.prod(self.shape):
            raise WeightStreamError("Tensor nnz exceeds its size")
        if self.encoding == "dense" and self.nnz != math.prod(self.shape):
            raise WeightStreamError("Dense tensor must contain every element")
        path = PurePosixPath(self.file)
        if (
            not self.file
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in self.file
        ):
            raise WeightStreamError("Unsafe payload path")
        if len(self.checksum) != 32 or any(
            c not in "0123456789abcdef" for c in self.checksum
        ):
            raise WeightStreamError("Expected xxh3_128 payload checksum")


@dataclass(frozen=True)
class WeightManifest:
    version: int
    parent_version: int | None
    kind: Literal["keyframe", "patch"]
    base_model: str
    architecture_hash: str
    tensors: tuple[TensorEntry, ...]
    trainer_step: int | None = None
    training_run_id: str | None = None
    created_at: str = dataclasses.field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    schema_version: int = 1

    def __post_init__(self) -> None:
        version_dir(self.version)
        if self.schema_version != 1 or self.kind not in ("keyframe", "patch"):
            raise WeightStreamError("Unsupported weight stream schema or kind")
        if self.parent_version is not None:
            version_dir(self.parent_version)
            if self.parent_version >= self.version:
                raise WeightStreamError("Parent version must precede version")
        if self.kind == "patch" and self.parent_version is None:
            raise WeightStreamError("Patch requires a parent version")
        if len({t.name for t in self.tensors}) != len(self.tensors):
            raise WeightStreamError("Duplicate tensor name")
        if not self.base_model or len(self.architecture_hash) != 64:
            raise WeightStreamError(
                "Manifest requires base model and architecture hash"
            )
        if self.kind == "keyframe" and any(t.encoding != "dense" for t in self.tensors):
            raise WeightStreamError("Keyframes must contain dense tensors")

    def to_json(self) -> bytes:
        return json.dumps(
            dataclasses.asdict(self), sort_keys=True, separators=(",", ":")
        ).encode()

    @classmethod
    def from_json(cls, data: bytes | str) -> WeightManifest:
        try:
            obj = json.loads(data)
            obj["tensors"] = tuple(_entry_from_dict(t) for t in obj["tensors"])
            return cls(**obj)
        except (TypeError, KeyError, ValueError) as exc:
            raise WeightStreamError(f"Invalid weight manifest: {exc}") from exc


def pointer_json(version: int | None) -> bytes:
    if version is not None:
        version_dir(version)
    return json.dumps(
        {"schema_version": 1, "version": version}, sort_keys=True
    ).encode()


def read_pointer(data: bytes | str) -> int | None:
    try:
        obj = json.loads(data)
        if obj["schema_version"] != 1:
            raise ValueError("Unsupported pointer schema")
        version = obj["version"]
        if version is not None:
            version_dir(version)
        return version
    except (ValueError, KeyError, TypeError) as exc:
        raise WeightStreamError(f"Invalid weight pointer: {exc}") from exc


def architecture_hash(config: bytes, tensors: Any) -> str:
    """Hash canonical config and all checkpoint tensor names/shapes/dtypes."""
    schema = sorted(
        (name, list(t.shape), str(t.dtype).removeprefix("torch."))
        for name, t in tensors
    )
    canonical = json.dumps(
        {"config": json.loads(config), "tensors": schema},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _entry_from_dict(data: dict[str, Any]) -> TensorEntry:
    values: dict[str, Any] = dict(data)
    values["shape"] = tuple(values["shape"])
    return TensorEntry(**values)
