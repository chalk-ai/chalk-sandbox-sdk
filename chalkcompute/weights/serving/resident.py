"""Bounded, direct sparse updates for an explicitly bound resident tensor layout.

The caller owns runtime admission, request draining and loading the initial
keyframe. This updater never invokes a model weight loader. It accepts absolute
sparse patches against the immediately observed parent version; a keyframe or
dense patch requires restarting the replica.

Importing this module does not import Torch or a serving engine. Constructing a
resident requires Torch and the exact CUDA layout validated below.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from chalkcompute._weights_codec import read_patch, verify_payloads
from chalkcompute.weights.layout import TensorSpec, WeightLayout, WeightSchema
from chalkcompute._weights_format import (
    WeightManifest,
    WeightStreamError,
    architecture_hash,
    safe_path,
)

PROFILE = "resident-bf16-single-cuda-v1"
MAX_MANIFEST_BYTES = 1024**2
MAX_PAYLOAD_BYTES = 512 * 1024**2
_CHUNK_ELEMENTS = 1024**2


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WeightStreamError("Duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise WeightStreamError("Nonfinite JSON value")


def _json(data: bytes) -> Any:
    try:
        return json.loads(
            data, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
    except (UnicodeError, ValueError, RecursionError) as error:
        raise WeightStreamError("Invalid bounded JSON") from error


def validate_config(config: dict[str, Any] | bytes) -> dict[str, Any]:
    """Validate bounded JSON configuration without assuming a model architecture.

    Compatibility is established by the manifest architecture hash, explicit
    logical schema and bound layout, rather than model-name heuristics.
    """
    if isinstance(config, bytes):
        if len(config) > MAX_MANIFEST_BYTES:
            raise WeightStreamError("Model config exceeds size bound")
        config = _json(config)
    if not isinstance(config, dict):
        raise WeightStreamError("Model config must be a JSON object")
    try:
        if len(json.dumps(config, allow_nan=False).encode()) > MAX_MANIFEST_BYTES:
            raise WeightStreamError("Model config exceeds size bound")
    except (TypeError, ValueError, RecursionError) as error:
        raise WeightStreamError("Invalid model config JSON") from error
    return dict(config)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _valid_sha256(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def load_manifest(
    directory: Path, expected_version: int, expected_sha256: str
) -> WeightManifest:
    """Read at most 1MiB and bind the exact manifest bytes to the requested SHA."""
    if type(expected_version) is not int or expected_version < 0:
        raise WeightStreamError("Expected version must be a nonnegative integer")
    if not _valid_sha256(expected_sha256):
        raise WeightStreamError("Expected manifest SHA256 required")
    raw = _read_bounded(Path(directory), "manifest.json")
    if _sha256(raw) != expected_sha256:
        raise WeightStreamError("Manifest size or SHA256 mismatch")
    # Reject duplicate fields and nonfinite JSON before dataclass parsing.
    _json(raw)
    manifest = WeightManifest.from_json(raw)
    if manifest.version != expected_version:
        raise WeightStreamError("Manifest version differs from requested version")
    return manifest


def _read_bounded(directory: Path, name: str) -> bytes:
    path = safe_path(directory, name)
    if not stat.S_ISREG(path.stat().st_mode):
        raise WeightStreamError("Manifest/config must be a regular file")
    with path.open("rb") as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise WeightStreamError("Manifest/config exceeds size bound")
    return raw


def read_profile_file(directory: Path, name: str) -> bytes:
    """Read a regular, contained profile input with the hard 1MiB bound."""
    if name not in ("config.json", "manifest.json", "layout.json"):
        raise WeightStreamError(
            "Only resident config, manifest and layout inputs are supported"
        )
    return _read_bounded(Path(directory), name)


def _torch() -> Any:
    try:
        import torch
    except ImportError as error:
        raise ImportError(
            "Install chalkcompute[serving] for resident updates"
        ) from error
    return torch


def _process_start_ticks() -> str:
    return Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]


class ResidentWeights:
    """Update a caller-loaded and drained single-CUDA BF16 model in place.

    ``initial`` must describe the complete loaded keyframe. ``layout`` must be
    explicitly bound to that logical schema. Call :meth:`verify_initial` before
    accepting updates. Engine adapters separately enforce runtime admission and
    request draining. This class never calls a checkpoint/model weight loader.

    Sparse updates are limited to 2% of the declared model elements (rounded
    down), with at most 512MiB of compressed payload files per publication.
    """

    def __init__(
        self, model: Any, config: bytes, initial: WeightManifest, layout: WeightLayout
    ):
        if not isinstance(config, bytes):
            raise WeightStreamError("Initial config must be its checkpoint JSON bytes")
        validate_config(config)
        try:
            self.schema = WeightSchema.from_manifest(initial)
        except (TypeError, ValueError) as error:
            raise WeightStreamError(
                "Complete valid initial keyframe schema required"
            ) from error
        self.schema_sha256 = self.schema.sha256
        if layout.source_schema_sha256 != self.schema_sha256:
            raise WeightStreamError("Layout must be bound to the initial source schema")
        self.layout_sha256 = layout.sha256
        self._logical_schema = {
            entry.name: tuple(entry.shape) for entry in initial.tensors
        }
        self._total_elements = sum(
            math.prod(shape) for shape in self._logical_schema.values()
        )
        self._max_sparse_elements = self._total_elements // 50
        self._architecture_hash = architecture_hash(
            config,
            (
                (name, SimpleNamespace(shape=shape, dtype="bfloat16"))
                for name, shape in self._logical_schema.items()
            ),
        )
        if (
            initial.kind != "keyframe"
            or initial.architecture_hash != self._architecture_hash
            or not self._logical_schema
            or any(
                entry.dtype != "bfloat16"
                or entry.encoding != "dense"
                or entry.nnz != math.prod(entry.shape)
                or entry.nnz == 0
                for entry in initial.tensors
            )
        ):
            raise WeightStreamError("Complete nonempty BF16 keyframe required")
        self.model = model
        self._config = config
        self._initial = initial
        self._initial_verified = False
        # Do not let named_parameters' usual duplicate-removal hide aliases.
        named = list(model.named_parameters(remove_duplicate=False))
        if len({name for name, _ in named}) != len(named):
            raise WeightStreamError("Unique resident parameter names required")
        self._parameters = dict(named)
        target_schema = WeightSchema(
            tuple(
                TensorSpec(
                    name,
                    tuple(parameter.shape),
                    str(parameter.dtype).removeprefix("torch."),
                )
                for name, parameter in named
            )
        )
        try:
            self._compiled = layout.compile(self.schema, target_schema)
        except (TypeError, ValueError) as error:
            raise WeightStreamError("Resident layout compilation failed") from error
        self._native_schema = {
            name: tuple(parameter.shape) for name, parameter in named
        }
        self._identity = self._snapshot()
        self.identity_sha256 = _sha256(
            json.dumps(self._identity, sort_keys=True, separators=(",", ":")).encode()
        )
        self._publication: tuple[int, str | None] = (initial.version, None)
        self._poisoned = False
        self._lock = threading.Lock()

    @property
    def version(self) -> int:
        return self._publication[0]

    @property
    def poisoned(self) -> bool:
        """A poisoned resident must be restarted, including after a lost ACK."""
        return self._poisoned

    @property
    def initial_verified(self) -> bool:
        return self._initial_verified

    def _snapshot(self) -> dict[str, Any]:
        torch = _torch()
        named = list(self.model.named_parameters(remove_duplicate=False))
        live = dict(named)
        if (
            len(live) != len(named)
            or set(live) != set(self._native_schema)
            or any(live[name] is not self._parameters.get(name) for name in live)
        ):
            raise WeightStreamError("Exact live parameter catalog required")
        records = {}
        for name, parameter in live.items():
            if (
                tuple(parameter.shape) != self._native_schema[name]
                or parameter.dtype != torch.bfloat16
                or parameter.device.type != "cuda"
                or not parameter.is_contiguous()
            ):
                raise WeightStreamError("Contiguous BF16 CUDA resident layout required")
            storage = parameter.untyped_storage()
            if (
                parameter.storage_offset() != 0
                or storage.nbytes() != parameter.numel() * parameter.element_size()
                or parameter.data_ptr() != storage.data_ptr()
            ):
                raise WeightStreamError("Whole unaliased native allocations required")
            records[name] = {
                "parameter_id": id(parameter),
                "storage_ptr": storage.data_ptr(),
                "storage_bytes": storage.nbytes(),
                "data_ptr": parameter.data_ptr(),
                "shape": list(parameter.shape),
                "stride": list(parameter.stride()),
                "storage_offset": parameter.storage_offset(),
                "device": str(parameter.device),
            }
        if (
            len({record["storage_ptr"] for record in records.values()}) != len(live)
            or len({record["parameter_id"] for record in records.values()}) != len(live)
            or len({record["device"] for record in records.values()}) != 1
        ):
            raise WeightStreamError("Unaliased parameters on one CUDA device required")
        return {
            "pid": os.getpid(),
            "start_ticks": _process_start_ticks(),
            "model_id": id(self.model),
            "parameters": records,
            "schema_sha256": self.schema_sha256,
            "layout_sha256": self.layout_sha256,
        }

    def _assert_identity(self) -> None:
        if self._poisoned:
            raise WeightStreamError("Resident is poisoned; restart replica")
        try:
            if self._snapshot() != self._identity:
                raise WeightStreamError("Resident identity changed")
        except BaseException:
            self._poisoned = True
            raise

    def verify_initial(self, directory: Path) -> dict[str, Any]:
        """Prove the loaded base equals all keyframe bytes before accepting deltas.

        This startup-only CPU byte audit reads the complete keyframe and copies
        each resident parameter to CPU. Its cost is excluded from delta timings.
        The layout inverse reconstructs logical tensors independently of the sparse
        coordinate mapper used by the indexed writer.
        """
        from contextlib import ExitStack

        from safetensors import safe_open

        torch = _torch()
        directory = Path(directory)
        with self._lock:
            self._assert_identity()
            if self.version != self._initial.version:
                raise WeightStreamError(
                    "Cannot rebind an advanced resident to its initial version"
                )
            self._initial_verified = False
            raw_manifest = _read_bounded(directory, "manifest.json")
            _json(raw_manifest)
            if WeightManifest.from_json(raw_manifest) != self._initial:
                raise WeightStreamError(
                    "Initial manifest differs from the bound keyframe"
                )
            if _read_bounded(directory, "config.json") != self._config:
                raise WeightStreamError(
                    "Initial config differs from the bound keyframe"
                )
            by_file: dict[Path, set[str]] = {}
            for entry in self._initial.tensors:
                path = safe_path(directory, entry.file)
                info = path.stat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or not entry.file.endswith(".safetensors")
                    or entry.offset != 0
                    or entry.length != info.st_size
                ):
                    raise WeightStreamError(
                        "Initial entries must reference whole safetensors files"
                    )
                by_file.setdefault(path, set()).add(entry.name)
            # Exact BF16 payload size plus a bounded reserve for safetensors
            # headers; unrelated files are never included in this audit.
            if (
                sum(path.stat().st_size for path in by_file)
                > self._total_elements * 2 + 16 * 1024**2
            ):
                raise WeightStreamError("Initial safetensors exceed model size bound")
            verify_payloads(directory, self._initial)
            records = {}
            with ExitStack() as stack:
                sources = {}
                for path, expected in by_file.items():
                    source = stack.enter_context(
                        safe_open(path, framework="pt", device="cpu")
                    )
                    if set(source.keys()) != expected:
                        raise WeightStreamError(
                            "Initial safetensors catalog differs from manifest"
                        )
                    for name in expected:
                        view = source.get_slice(name)
                        if (
                            tuple(view.get_shape()) != self._logical_schema[name]
                            or view.get_dtype() != "BF16"
                        ):
                            raise WeightStreamError(
                                "Initial safetensors shape or dtype mismatch"
                            )
                        sources[name] = source
                if set(sources) != set(self._logical_schema):
                    raise WeightStreamError("Complete initial logical catalog required")
                for name in self._logical_schema:
                    actual = self._compiled.read_source(
                        name, lambda target: self._copy_to_cpu(self._parameters[target])
                    )
                    if (
                        tuple(actual.shape) != self._logical_schema[name]
                        or actual.dtype != torch.bfloat16
                        or actual.device.type != "cpu"
                    ):
                        raise WeightStreamError(
                            "Independent initial resident layout mismatch"
                        )
                    reference = sources[name].get_tensor(name)
                    actual_bits = actual.contiguous().reshape(-1).view(torch.uint8)
                    reference_bits = (
                        reference.contiguous().reshape(-1).view(torch.uint8)
                    )
                    if not torch.equal(actual_bits, reference_bits):
                        raise WeightStreamError(
                            "Loaded resident differs from initial keyframe bytes"
                        )
                    digest = hashlib.sha256()
                    for first in range(0, actual_bits.numel(), 4 * 1024**2):
                        digest.update(
                            actual_bits[first : first + 4 * 1024**2].numpy().tobytes()
                        )
                    records[name] = digest.hexdigest()
                    del actual, reference, actual_bits, reference_bits
            if set(records) != set(self._logical_schema):
                raise WeightStreamError("Complete initial resident byte audit required")
            verify_payloads(directory, self._initial)
            if (
                _read_bounded(directory, "manifest.json") != raw_manifest
                or _read_bounded(directory, "config.json") != self._config
            ):
                raise WeightStreamError("Initial source changed during verification")
            self._assert_identity()
            self._initial_verified = True
            return {
                "ok": True,
                "profile": PROFILE,
                "version": self.version,
                "logical_tensors": len(records),
                "base_sha256": _sha256(json.dumps(records, sort_keys=True).encode()),
                "identity_sha256": self.identity_sha256,
                "schema_sha256": self.schema_sha256,
                "layout_sha256": self.layout_sha256,
                "manifest_sha256": _sha256(raw_manifest),
                "config_sha256": _sha256(self._config),
            }

    def _copy_to_cpu(self, parameter: Any) -> Any:
        try:
            return parameter.detach().cpu()
        except BaseException:
            # A failed device transfer/synchronization leaves runtime health
            # uncertain, even though the initial audit performs no writes.
            self._poisoned = True
            raise

    def _validate_patch(self, directory: Path, manifest: WeightManifest) -> int:
        if manifest.kind != "patch" or any(
            entry.encoding != "sparse" for entry in manifest.tensors
        ):
            raise WeightStreamError(
                "Keyframes and dense patches require replica restart"
            )
        if (
            manifest.base_model != self._initial.base_model
            or manifest.architecture_hash != self._architecture_hash
            or any(
                entry.name not in self._logical_schema
                or entry.shape != self._logical_schema[entry.name]
                or entry.dtype != "bfloat16"
                for entry in manifest.tensors
            )
        ):
            raise WeightStreamError("Patch differs from bound resident schema")
        nnz = sum(entry.nnz for entry in manifest.tensors)
        if nnz > self._max_sparse_elements:
            raise WeightStreamError("Patch exceeds resident sparse element bound")
        files = {safe_path(directory, entry.file) for entry in manifest.tensors}
        metadata = {path: path.stat() for path in files}
        if any(not stat.S_ISREG(info.st_mode) for info in metadata.values()):
            raise WeightStreamError("Payload must be a regular file")
        sizes = {path: info.st_size for path, info in metadata.items()}
        if sum(sizes.values()) > MAX_PAYLOAD_BYTES or any(
            entry.offset + entry.length > sizes[safe_path(directory, entry.file)]
            for entry in manifest.tensors
        ):
            raise WeightStreamError("Patch exceeds compressed payload bounds")
        return nnz

    def _prepare(self, directory: Path, manifest: WeightManifest) -> list[Any]:
        torch = _torch()
        verify_payloads(directory, manifest)
        prepared = []
        for entry in manifest.tensors:
            patch = read_patch(directory, entry)
            indices, values = patch.indices, patch.values
            if (
                indices is None
                or indices.dtype != torch.int64
                or indices.device.type != "cpu"
                or indices.ndim != 1
                or values.dtype != torch.bfloat16
                or values.device.type != "cpu"
                or values.ndim != 1
                or values.numel() != entry.nnz
                or indices.numel() != entry.nnz
                or not bool(torch.isfinite(values).all())
                or (
                    entry.nnz
                    and (
                        int(indices[0]) < 0
                        or int(indices[-1]) >= math.prod(entry.shape)
                        or bool((indices[1:] <= indices[:-1]).any())
                    )
                )
            ):
                raise WeightStreamError(
                    "Finite sorted sparse BF16 CPU payload required"
                )
            for route in self._compiled.map_indices(entry.name, indices):
                prepared.append(
                    (
                        self._parameters[route.target],
                        route.indices,
                        values[route.source_positions],
                    )
                )
        return prepared

    def _synchronize(self) -> None:
        torch = _torch()
        device = next(iter(self._parameters.values())).device
        torch.cuda.synchronize(device)

    def _write(self, prepared: list[Any]) -> tuple[int, int]:
        torch = _torch()
        from torch.utils._python_dispatch import TorchDispatchMode

        storages = {
            parameter.untyped_storage().data_ptr()
            for parameter in self._parameters.values()
        }
        writes = 0
        written = 0

        class IndexedWritesOnly(TorchDispatchMode):
            def __torch_dispatch__(
                self,
                func: Any,
                types: Any,
                args: tuple[Any, ...] = (),
                kwargs: dict[str, Any] | None = None,
            ) -> Any:
                nonlocal writes, written
                if func._schema.is_mutable:
                    if (
                        str(func) != "aten.index_copy_.default"
                        or args[0].untyped_storage().data_ptr() not in storages
                    ):
                        raise WeightStreamError(
                            "Only indexed resident writes permitted"
                        )
                    writes += 1
                    written += args[3].numel()
                return func(*args, **(kwargs or {}))

        with torch.no_grad(), IndexedWritesOnly():
            for parameter, mapped_indices, mapped_values in prepared:
                for first in range(0, mapped_indices.numel(), _CHUNK_ELEMENTS):
                    stop = first + _CHUNK_ELEMENTS
                    indices = mapped_indices[first:stop].to(parameter.device)
                    values = mapped_values[first:stop].to(parameter.device)
                    parameter.reshape(-1).index_copy_(0, indices, values)
        return writes, written

    def apply(
        self,
        directory: Path,
        expected_version: int,
        expected_manifest_sha256: str,
    ) -> dict[str, Any]:
        """Verify all payloads before any write; retry an exact ACK without writes.

        Identity changes and any exception during synchronization or mutation
        permanently poison this instance. Validation refusals before that region
        leave a healthy resident available for a corrected request.
        """
        started = time.monotonic()
        with self._lock:
            self._assert_identity()
            if not self._initial_verified:
                raise WeightStreamError(
                    "Initial resident byte audit required before applying patches"
                )
            directory = Path(directory)
            manifest = load_manifest(
                directory, expected_version, expected_manifest_sha256
            )
            nnz = self._validate_patch(directory, manifest)
            replay = manifest.version == self.version
            if replay:
                if expected_manifest_sha256 != self._publication[1]:
                    raise WeightStreamError(
                        "Current version has different manifest content"
                    )
            elif (
                manifest.parent_version != self.version
                or manifest.version <= self.version
            ):
                raise WeightStreamError("Patch parent differs from resident version")
            prepared = self._prepare(directory, manifest)
            self._assert_identity()
            decode_seconds = time.monotonic() - started
            writes = 0
            scatter_seconds = 0.0
            mutation_region = not replay and nnz > 0
            try:
                if mutation_region:
                    self._synchronize()
                    scatter_started = time.monotonic()
                    writes, written = self._write(prepared)
                    self._synchronize()
                    scatter_seconds = time.monotonic() - scatter_started
                    if written != nnz:
                        raise WeightStreamError("Sparse write element count mismatch")
                    self._assert_identity()
                ack = {
                    "ok": True,
                    "version": manifest.version,
                    "manifest_sha256": expected_manifest_sha256,
                    "identity_sha256": self.identity_sha256,
                    "schema_sha256": self.schema_sha256,
                    "layout_sha256": self.layout_sha256,
                    "profile": PROFILE,
                    "sparse_elements": 0 if replay else nnz,
                    "index_copy_calls": writes,
                    "full_loader_calls": 0,
                    "replayed": replay,
                    "decode_validate_seconds": decode_seconds,
                    "scatter_and_sync_seconds": scatter_seconds,
                    "total_apply_seconds": time.monotonic() - started,
                }
                # Commit version and content binding together only after ACK
                # construction succeeds. Lost transport ACKs can replay this
                # exact publication; local failures after writes poison it.
                self._publication = (manifest.version, expected_manifest_sha256)
                return ack
            except BaseException:
                if mutation_region:
                    self._poisoned = True
                raise
