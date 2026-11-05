"""Single-writer, immutable weight publications backed by a ChalkFS volume."""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from copy import deepcopy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chalkcompute._volume import Volume, VolumeFileNotFoundError
from chalkcompute._weights_codec import (
    apply_patch,
    checksum,
    decode_patch,
    diff,
    encode_patch,
)
from chalkcompute._weights_format import (
    TensorEntry,
    WeightManifest,
    WeightStreamError,
    architecture_hash,
    pointer_json,
    read_pointer,
    safe_path,
    version_dir,
)

if TYPE_CHECKING:
    import torch

    from chalkcompute._connect import ConnectClient
    from chalkcompute._weight_stream_metadata import _WeightStreamMetadata


@dataclass(frozen=True)
class MaterializedWeightCheckpoint:
    """A full checkpoint committed to one immutable output volume snapshot."""

    volume_name: str
    volume_version_id: int
    version: int
    source_volume_version_id: int


class WeightStream:
    """Versioned model weights with one active publisher and independent PIN writes.

    Volumes preserve immutable versions; ``prune`` removes paths from the live
    ref only and does not reclaim historical volume snapshots. Publish from one
    trainer process per stream. Multiple distributed/FSDP ranks must gather their
    checkpoint into that writer before publishing.
    """

    def __init__(
        self, name: str, base_model: str | None = None, *, volume: Volume | None = None
    ) -> None:
        self.name = name
        self.base_model = base_model
        self._volume = volume if volume is not None else Volume(name)
        if self._volume.ref != "main" or self._volume.version_id is not None:
            raise ValueError("Weight streams require a mutable main volume ref")
        self._lock = threading.RLock()
        self._metadata: _WeightStreamMetadata | None = None

    @property
    def volume(self) -> Volume:
        """Mount this volume at a mutable ``ref_name='main'`` on serving replicas."""
        return self._volume

    def create(self) -> WeightStream:
        self._volume.reload()
        return self

    @property
    def id(self) -> str | None:
        """Registered control-plane ID, or None for explicit volume-only usage."""
        return self._metadata.id if self._metadata is not None else None

    def register(
        self, *, client: ConnectClient | None = None, max_snapshots: int = 1000
    ) -> WeightStream:
        """Register/attach this volume and reconcile its immutable publications.

        Without registration the stream remains a volume-only publisher. The
        optional client is borrowed; the caller retains its cleanup obligation.
        Registration uses the volume's authenticated environment by default.
        """
        from chalkcompute._weight_stream_metadata import register

        with self._lock:
            if self._metadata is None:
                self._metadata = register(self, client or self._volume._client._connect)
            self._metadata.reconcile(self, max_snapshots=max_snapshots)
        return self

    def reconcile(self, *, max_snapshots: int = 1000) -> list[int]:
        """Retry pending metadata, newest first, using original volume snapshots.

        Manifests and immutable volume history form the durable retry outbox;
        no in-process queue is required. CURRENT/PIN are never changed here.
        """
        with self._lock:
            if self._metadata is None:
                raise WeightStreamError(
                    "Register the stream before reconciling metadata"
                )
            return self._metadata.reconcile(self, max_snapshots=max_snapshots)

    @property
    def reconciliation_stats(self) -> dict[str, int | float]:
        """Last reconciliation's snapshot/read/record counts and elapsed seconds."""
        return dict(self._metadata.last_reconcile_stats) if self._metadata else {}

    def _record_committed(self, version: int, snapshot_id: int) -> None:
        if self._metadata is not None:
            from chalkcompute._weight_stream_metadata import WeightMetadataPending

            try:
                self._metadata.record(
                    self, self._volume.at_version(snapshot_id), version
                )
            except Exception as error:
                raise WeightMetadataPending(version, snapshot_id) from error

    def _snapshot(self) -> Volume:
        self._volume.reload()
        return self._volume.at_version(self._volume.version.id)

    @staticmethod
    def _read_current(snapshot: Volume) -> int | None:
        try:
            return read_pointer(snapshot.read_file("CURRENT"))
        except VolumeFileNotFoundError:
            return None

    def current(self) -> int | None:
        self._volume.reload()
        # A new volume need not yet have an immutable snapshot.
        try:
            return read_pointer(self._volume.read_file("CURRENT"))
        except VolumeFileNotFoundError:
            return None

    def versions(self) -> list[WeightManifest]:
        snapshot = self._snapshot()
        manifests = [
            WeightManifest.from_json(snapshot.read_file(f.path))
            for f in snapshot.listdir()
            if f.path.endswith("/manifest.json")
        ]
        return sorted(manifests, key=lambda item: item.version)

    def publisher(
        self,
        *,
        keyframe_every: int = 50,
        density_threshold: float = 0.3,
        config: Mapping[str, Any] | None = None,
        training_run_id: str | None = None,
    ) -> WeightPublisher:
        return WeightPublisher(
            self,
            keyframe_every=keyframe_every,
            density_threshold=density_threshold,
            config=config,
            training_run_id=training_run_id,
        )

    def pin(self, version: int) -> None:
        with self._lock:
            if self._metadata is not None:
                self._metadata.pin(version)
                return
            snapshot = self._snapshot()
            target = WeightManifest.from_json(
                snapshot.read_file(f"{version_dir(version)}/manifest.json")
            )
            self._chain(snapshot, target.version)
            self._volume.put_file("PIN", pointer_json(version))

    def unpin(self) -> None:
        # A tombstone avoids races between a deletion and a concurrent publication.
        with self._lock:
            if self._metadata is not None:
                self._metadata.pin(None)
                return
            self._volume.put_file("PIN", pointer_json(None))

    def pinned(self) -> int | None:
        snapshot = self._snapshot()
        try:
            return read_pointer(snapshot.read_file("PIN"))
        except VolumeFileNotFoundError:
            return None

    @staticmethod
    def _chain(snapshot: Volume, version: int) -> list[WeightManifest]:
        chain = []
        target_hash = None
        while True:
            manifest = WeightManifest.from_json(
                snapshot.read_file(f"{version_dir(version)}/manifest.json")
            )
            if manifest.version != version:
                raise WeightStreamError("Manifest version does not match directory")
            target_hash = target_hash or manifest.architecture_hash
            if manifest.architecture_hash != target_hash:
                raise WeightStreamError("Architecture changed within replay chain")
            chain.append(manifest)
            if manifest.kind == "keyframe":
                return list(reversed(chain))
            assert manifest.parent_version is not None
            version = manifest.parent_version

    def materialize(
        self,
        version: int,
        destination: str | Path,
        *,
        source_volume_version_id: int | None = None,
    ) -> Path:
        """Reconstruct an exact HF checkpoint for audits or model promotion.

        The destination must be absent. Work is staged in a sibling directory,
        so failures never leave a checkpoint that looks complete. Set
        ``source_volume_version_id`` to replay entirely from that immutable
        snapshot, independent of CURRENT, PIN, and pruning on main.
        """
        from safetensors.torch import load_file, save_file

        destination = Path(destination)
        if destination.exists():
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source_volume_version_id is not None:
            if (
                isinstance(source_volume_version_id, bool)
                or not isinstance(source_volume_version_id, int)
                or source_volume_version_id < 0
            ):
                raise ValueError(
                    "source_volume_version_id must be a nonnegative integer"
                )
            snapshot = self._volume.at_version(source_volume_version_id)
        else:
            snapshot = self._snapshot()
        chain = self._chain(snapshot, version)
        base = chain[0]
        with tempfile.TemporaryDirectory(
            prefix=".weight-materialize-", dir=destination.parent
        ) as staging:
            stage = Path(staging)
            state = {}
            prefix = version_dir(base.version)
            for f in snapshot.listdir(prefix):
                relative = str(Path(f.path).relative_to(prefix))
                if f.kind == "directory" or relative == "manifest.json":
                    continue
                path = safe_path(stage, relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(snapshot.read_file(f.path))
            from chalkcompute._weights_codec import verify_payloads

            verify_payloads(stage, base)
            for filename in sorted({e.file for e in base.tensors}):
                shard = load_file(str(safe_path(stage, filename)))
                if state.keys() & shard.keys():
                    raise WeightStreamError("Duplicate tensor in keyframe shards")
                state.update(shard)
            expected = {entry.name: entry for entry in base.tensors}
            if state.keys() != expected.keys() or any(
                tuple(tensor.shape) != expected[name].shape
                or str(tensor.dtype).removeprefix("torch.") != expected[name].dtype
                for name, tensor in state.items()
            ):
                raise WeightStreamError("Keyframe tensors do not match manifest")
            if (
                architecture_hash((stage / "config.json").read_bytes(), state.items())
                != base.architecture_hash
            ):
                raise WeightStreamError(
                    "Keyframe architecture does not match config and tensors"
                )
            for manifest in chain[1:]:
                by_file: dict[str, list[TensorEntry]] = {}
                for entry in manifest.tensors:
                    by_file.setdefault(entry.file, []).append(entry)
                for filename, entries in by_file.items():
                    # A publisher packs all tensors into one shared patches.bin.
                    # Read each blob once, then release it before the next file.
                    blob = snapshot.read_file(
                        f"{version_dir(manifest.version)}/{filename}"
                    )
                    for entry in entries:
                        payload = blob[entry.offset : entry.offset + entry.length]
                        apply_patch(state[entry.name], decode_patch(entry, payload))
                    del blob, payload
            # Replace original weight shards/index with one deterministic checkpoint.
            for filename in {e.file for e in base.tensors}:
                safe_path(stage, filename).unlink()
            index = stage / "model.safetensors.index.json"
            index.unlink(missing_ok=True)
            save_file(state, str(stage / "model.safetensors"))
            (stage / "chalk-weight-version.json").write_bytes(pointer_json(version))
            os.rename(stage, destination)
        return destination

    def materialize_to_volume(
        self,
        version: int,
        *,
        source_volume_version_id: int,
        destination: Volume,
    ) -> MaterializedWeightCheckpoint:
        """Write a complete HF checkpoint to an empty, caller-owned output volume.

        Source and output commit IDs are immutable. The caller owns output volume
        cleanup, including on error; this method never deletes an external volume.
        The output volume must have a single writer during this operation.
        """
        if (
            isinstance(source_volume_version_id, bool)
            or not isinstance(source_volume_version_id, int)
            or source_volume_version_id < 0
        ):
            raise ValueError("source_volume_version_id must be a nonnegative integer")
        if destination.name == self._volume.name:
            raise WeightStreamError("Checkpoint output must use a different volume")
        if destination.version_id is not None or destination.ref != "main":
            raise WeightStreamError(
                "Checkpoint output requires a mutable main-ref volume"
            )
        destination.reload()
        if destination.listdir():
            raise WeightStreamError("Checkpoint output volume must be empty")
        with tempfile.TemporaryDirectory(
            prefix="chalk-weight-checkpoint-"
        ) as directory:
            folder = self.materialize(
                version,
                Path(directory) / "checkpoint",
                source_volume_version_id=source_volume_version_id,
            )
            try:
                config = json.loads((folder / "config.json").read_bytes())
            except (OSError, ValueError) as error:
                raise WeightStreamError(
                    "Checkpoint requires valid config.json"
                ) from error
            if not isinstance(config, dict):
                raise WeightStreamError("Checkpoint config.json must be an object")
            tokenizer_files = (
                "tokenizer.json",
                "tokenizer.model",
                "spiece.model",
                "vocab.txt",
            )
            if not (
                any((folder / name).is_file() for name in tokenizer_files)
                or (
                    (folder / "vocab.json").is_file()
                    and (folder / "merges.txt").is_file()
                )
                or any(folder.glob("*.tiktoken"))
            ):
                raise WeightStreamError("Checkpoint requires tokenizer assets")
            (folder / "chalk-weight-source.json").write_text(
                json.dumps(
                    {
                        "volume_name": self._volume.name,
                        "volume_version_id": source_volume_version_id,
                        "version": version,
                    },
                    sort_keys=True,
                )
            )
            files = sorted(path for path in folder.rglob("*") if path.is_file())
            if len(files) > 200:
                raise WeightStreamError(
                    "Checkpoint exceeds atomic 200-file output commit"
                )
            with destination.batch_upload(max_files_per_commit=200) as batch:
                for path in files:
                    batch.put_file(path.relative_to(folder).as_posix(), path)
            output_snapshot_id = destination.version.id
            if (
                isinstance(output_snapshot_id, bool)
                or not isinstance(output_snapshot_id, int)
                or output_snapshot_id < 0
            ):
                raise WeightStreamError(
                    "Output commit did not return an immutable snapshot ID"
                )
            # Verify the identity through an immutable read, never a reload of main.
            committed = destination.at_version(output_snapshot_id)
            if (
                committed.read_file("chalk-weight-source.json")
                != (folder / "chalk-weight-source.json").read_bytes()
            ):
                raise WeightStreamError(
                    "Output snapshot does not contain this checkpoint"
                )
        return MaterializedWeightCheckpoint(
            destination.name,
            output_snapshot_id,
            version,
            source_volume_version_id,
        )

    def prune(self, *, keep_keyframes: int = 2) -> list[int]:
        if keep_keyframes < 1:
            raise ValueError("keep_keyframes must be positive")
        with self._lock:
            manifests = self.versions()
            keys = [m.version for m in manifests if m.kind == "keyframe"]
            if len(keys) <= keep_keyframes:
                return []
            cutoff = keys[-keep_keyframes]
            pinned = self.pinned()
            if pinned is not None:
                snapshot = self._snapshot()
                cutoff = min(cutoff, self._chain(snapshot, pinned)[0].version)
            removed = [m.version for m in manifests if m.version < cutoff]
            for version in removed:
                for file in self._volume.listdir(version_dir(version)):
                    if file.kind != "directory":
                        self._volume.remove_file(file.path)
            return removed

    def seed_from_hf(self, repo: str, revision: str) -> int:
        """Publish pinned Hugging Face safetensors weights, config and tokenizer."""
        from huggingface_hub import snapshot_download
        from safetensors import safe_open

        if not revision:
            raise ValueError("An explicit Hugging Face revision is required")
        folder = Path(
            snapshot_download(
                repo_id=repo,
                revision=revision,
                allow_patterns=[
                    "*.safetensors",
                    "*.json",
                    "*.model",
                    "*.tiktoken",
                    "*.txt",
                ],
            )
        )
        with self._lock:
            if self.current() is not None:
                raise WeightStreamError("Weight stream is already seeded")
            config = (folder / "config.json").read_bytes()
            files = [
                (p.relative_to(folder).as_posix(), p)
                for p in folder.rglob("*")
                if p.is_file() and ".cache" not in p.relative_to(folder).parts
            ]
            if len(files) + 2 > 200:
                raise WeightStreamError(
                    "Seed exceeds atomic 200-file volume commit; re-shard checkpoint first"
                )
            tensors = []
            entries = []
            total_bytes = 0
            dtype_names = {
                "BF16": "bfloat16",
                "F16": "float16",
                "F32": "float32",
                "F64": "float64",
                "I64": "int64",
                "I32": "int32",
                "I16": "int16",
                "I8": "int8",
                "U8": "uint8",
                "BOOL": "bool",
                "F8_E4M3": "float8_e4m3fn",
                "F8_E5M2": "float8_e5m2",
            }
            for name, path in files:
                if not name.endswith(".safetensors"):
                    continue
                import xxhash

                digest = xxhash.xxh3_128()
                with path.open("rb") as stream:
                    while chunk := stream.read(4 * 1024 * 1024):
                        digest.update(chunk)
                length = path.stat().st_size
                total_bytes += length
                with safe_open(path, framework="pt", device="cpu") as handle:
                    for key in handle.keys():
                        tensor = handle.get_slice(key)
                        dtype = dtype_names.get(tensor.get_dtype())
                        if dtype is None:
                            raise WeightStreamError(
                                f"Unsupported checkpoint dtype: {tensor.get_dtype()}"
                            )
                        shape = tuple(tensor.get_shape())
                        entries.append(
                            TensorEntry(
                                key,
                                dtype,
                                shape,
                                "dense",
                                name,
                                0,
                                length,
                                math.prod(shape),
                                digest.hexdigest(),
                            )
                        )
                        tensors.append((key, _TensorSpec(shape, dtype)))
            if not entries:
                raise WeightStreamError("No safetensors checkpoint files found")
            self.base_model = self.base_model or repo
            manifest = WeightManifest(
                0,
                None,
                "keyframe",
                self.base_model,
                architecture_hash(config, tensors),
                tuple(entries),
            )
            snapshot_id = self._commit(manifest, files)
            self._record_committed(0, snapshot_id)
            return 0

    def _commit(
        self,
        manifest: WeightManifest,
        files: list[tuple[str, Path]],
        *,
        timings: dict[str, float] | None = None,
        commit_stats: dict[str, Any] | None = None,
    ) -> int:
        if len(files) + 2 > 200:
            raise WeightStreamError("Publication exceeds atomic 200-file volume commit")
        # Stale-parent rejection; one active publishing process is a contract.
        expected = manifest.parent_version
        if self.current() != expected:
            raise WeightStreamError(
                "Publisher is stale; recreate it from the latest checkpoint"
            )
        prefix = version_dir(manifest.version)
        # Reserve space for manifest and CURRENT, avoiding batch auto-flush splits.
        started = time.perf_counter()
        with self._volume.batch_upload(max_files_per_commit=200) as batch:
            for name, path in files:
                safe_path(Path("/weight-publication"), name)
                batch.put_file(f"{prefix}/{name}", path)
            batch.put(f"{prefix}/manifest.json", manifest.to_json())
            batch.put("CURRENT", pointer_json(manifest.version))
        # Batch upload retains the server-returned immutable commit version.
        # Capture it before reloading main, which may already include a PIN write.
        committed_snapshot_id = self._volume.version.id
        if commit_stats is not None:
            commit_stats.update(
                volume_version_id=committed_snapshot_id, status="refresh_failed"
            )
        if timings is not None:
            timings["upload_commit"] = time.perf_counter() - started
        started = time.perf_counter()
        try:
            if self.current() != manifest.version:
                raise WeightStreamError("Concurrent publisher detected after commit")
        finally:
            if timings is not None:
                timings["post_commit_refresh"] = time.perf_counter() - started
        if commit_stats is not None:
            commit_stats["status"] = "committed_unindexed"
        return committed_snapshot_id


class _TensorSpec:
    def __init__(self, shape: tuple[int, ...], dtype: str) -> None:
        self.shape, self.dtype = shape, dtype


class WeightPublisher:
    """Retain an independent prior state on the tensors' device for exact diffs.

    Initial publication is a keyframe. Resuming an existing stream requires
    ``resume(state_dict)`` with its exact current weights, avoiding an implicit
    multi-GB CPU reconstruction on trainer startup.
    """

    def __init__(
        self,
        stream: WeightStream,
        *,
        keyframe_every: int = 50,
        density_threshold: float = 0.3,
        config: Mapping[str, Any] | None = None,
        training_run_id: str | None = None,
    ) -> None:
        if keyframe_every < 1 or not 0 <= density_threshold <= 1:
            raise ValueError("Invalid keyframe cadence or density threshold")
        self.stream = stream
        self.keyframe_every = keyframe_every
        self.density_threshold = density_threshold
        self.training_run_id = training_run_id or os.environ.get(
            "CHALK_TRAINING_RUN_ID"
        )
        self._config = (
            json.dumps(dict(config), sort_keys=True).encode()
            if config is not None
            else None
        )
        self._previous: dict[str, torch.Tensor] | None = None
        self._version: int | None = None
        self._architecture: str | None = None
        self._last_publish_stats: dict[str, Any] | None = None

    @property
    def last_publish_stats(self) -> dict[str, Any] | None:
        """JSON-serializable metrics for the latest publication attempt.

        Timings measure host wall time without adding GPU synchronization.
        Native upload and commit run together and are reported as one phase.
        """
        return deepcopy(self._last_publish_stats)

    def resume(
        self,
        state_dict: Mapping[str, torch.Tensor] | Iterable[tuple[str, torch.Tensor]],
    ) -> None:
        """Resume only after checking a caller-provided exact checkpoint against CURRENT.

        Tensor bytes are verified against the materialized stream; this is an
        explicit costly operation, never a trust-based parent reassignment.
        """
        from safetensors.torch import load_file
        import torch

        state = _state(state_dict)
        current = self.stream.current()
        if current is None:
            raise WeightStreamError("Cannot resume an unseeded stream")
        with tempfile.TemporaryDirectory() as parent:
            folder = self.stream.materialize(current, Path(parent) / "checkpoint")
            expected = load_file(str(folder / "model.safetensors"))
            if state.keys() != expected.keys() or any(
                state[n].shape != expected[n].shape
                or state[n].dtype != expected[n].dtype
                or not torch.equal(
                    state[n].detach().cpu().contiguous().reshape(-1).view(torch.uint8),
                    expected[n].contiguous().reshape(-1).view(torch.uint8),
                )
                for n in state
            ):
                raise WeightStreamError(
                    "Resume state does not match current checkpoint"
                )
            self._config = (folder / "config.json").read_bytes()
            manifest = WeightManifest.from_json(
                self.stream._snapshot().read_file(
                    f"{version_dir(current)}/manifest.json"
                )
            )
            if (
                self.stream.base_model is not None
                and self.stream.base_model != manifest.base_model
            ):
                raise WeightStreamError(
                    "Stream base_model does not match the stored manifest"
                )
            self.stream.base_model = manifest.base_model
            resumed_architecture = architecture_hash(self._config, state.items())
            if resumed_architecture != manifest.architecture_hash:
                raise WeightStreamError(
                    "Resume architecture does not match the stored manifest"
                )
        self._previous = {n: t.detach().clone() for n, t in state.items()}
        self._version = current
        self._architecture = resumed_architecture

    def publish(
        self,
        state_dict: Mapping[str, torch.Tensor] | Iterable[tuple[str, torch.Tensor]],
        step: int | None = None,
    ) -> int:
        return self._publish(state_dict, step=step, force_keyframe=False)

    def publish_keyframe(
        self,
        state_dict: Mapping[str, torch.Tensor] | Iterable[tuple[str, torch.Tensor]],
        step: int | None = None,
    ) -> int:
        return self._publish(state_dict, step=step, force_keyframe=True)

    def _publish(self, state_dict, *, step, force_keyframe):
        import torch

        from chalkcompute._weight_stream_metadata import WeightMetadataPending

        started = time.perf_counter()
        stats: dict[str, Any] = {
            "status": "failed",
            "version": None,
            "volume_version_id": None,
            "torch_num_threads": torch.get_num_threads(),
            "torch_num_interop_threads": torch.get_num_interop_threads(),
            "phases_seconds": {
                name: 0.0
                for name in (
                    "validation",
                    "freeze",
                    "diff",
                    "encode",
                    "keyframe_write",
                    "upload_commit",
                    "post_commit_refresh",
                    "metadata",
                )
            },
        }
        self._last_publish_stats = stats
        try:
            result = self._publish_profiled(
                state_dict, step=step, force_keyframe=force_keyframe, stats=stats
            )
            stats["status"] = "published"
            return result
        except WeightMetadataPending:
            stats["status"] = "metadata_pending"
            raise
        finally:
            stats["total_seconds"] = time.perf_counter() - started

    def _publish_profiled(self, state_dict, *, step, force_keyframe, stats):
        from safetensors.torch import save_file

        with self.stream._lock:
            started = time.perf_counter()
            phases = stats["phases_seconds"]
            state = _state(state_dict)
            current = self.stream.current()
            if current != self._version:
                raise WeightStreamError(
                    "Publisher is stale; call resume with the exact current checkpoint"
                )
            version = 0 if current is None else current + 1
            if not self.stream.base_model:
                raise WeightStreamError("Set base_model before publishing")
            if self._config is None:
                raise WeightStreamError(
                    "Initial publication requires publisher(config=...) or resume()"
                )
            arch = architecture_hash(self._config, state.items())
            if self._architecture is not None and arch != self._architecture:
                raise WeightStreamError(
                    "Model architecture changed; create a new weight stream"
                )
            keyframe = (
                force_keyframe
                or self._previous is None
                or version % self.keyframe_every == 0
            )
            previous = self._previous
            total_elements = sum(t.numel() for t in state.values())
            stats.update(
                version=version,
                kind="keyframe" if keyframe else "patch",
                input_devices=sorted({str(t.device) for t in state.values()}),
                total_elements=total_elements,
                changed_elements=total_elements
                if previous is None
                else (None if keyframe else 0),
                transmitted_elements=0,
                payload_bytes=0,
            )
            phases["validation"] = time.perf_counter() - started
            started = time.perf_counter()
            # Freeze every tensor before encoding: training must synchronize its
            # optimizer while publishing. Clone prevents later caller mutation.
            frozen = {name: tensor.detach().clone() for name, tensor in state.items()}
            phases["freeze"] = time.perf_counter() - started
            with tempfile.TemporaryDirectory(
                prefix="chalk-weight-publish-"
            ) as directory:
                folder = Path(directory)
                entries = []
                files = []
                if keyframe:
                    started = time.perf_counter()
                    path = folder / "model.safetensors"
                    save_file(
                        {n: t.cpu().contiguous() for n, t in frozen.items()}, str(path)
                    )
                    import xxhash

                    digest = xxhash.xxh3_128()
                    with path.open("rb") as reader:
                        while chunk := reader.read(4 * 1024 * 1024):
                            digest.update(chunk)
                    for name, tensor in frozen.items():
                        entries.append(
                            TensorEntry(
                                name,
                                str(tensor.dtype).removeprefix("torch."),
                                tuple(tensor.shape),
                                "dense",
                                path.name,
                                0,
                                path.stat().st_size,
                                tensor.numel(),
                                digest.hexdigest(),
                            )
                        )
                    files.append((path.name, path))
                    config_path = folder / "config.json"
                    config_path.write_bytes(self._config)
                    files.append((config_path.name, config_path))
                    if current is not None:
                        snapshot = self.stream._snapshot()
                        base = self.stream._chain(snapshot, current)[0]
                        prefix = version_dir(base.version)
                        for f in snapshot.listdir(prefix):
                            name = str(Path(f.path).relative_to(prefix))
                            if (
                                f.kind == "directory"
                                or name in ("manifest.json", "config.json")
                                or name.endswith(".safetensors")
                                or name == "model.safetensors.index.json"
                            ):
                                continue
                            path = safe_path(folder, name)
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_bytes(snapshot.read_file(f.path))
                            files.append((name, path))
                    phases["keyframe_write"] = time.perf_counter() - started
                    stats["transmitted_elements"] = total_elements
                    stats["payload_bytes"] = (
                        (folder / "model.safetensors").stat().st_size
                    )
                else:
                    path = folder / "patches.bin"
                    with path.open("wb") as writer:
                        assert previous is not None
                        for name, tensor in frozen.items():
                            started = time.perf_counter()
                            patch = diff(
                                previous[name],
                                tensor,
                                name=name,
                                density_threshold=self.density_threshold,
                            )
                            phases["diff"] += time.perf_counter() - started
                            if patch is None:
                                continue
                            started = time.perf_counter()
                            payload = encode_patch(patch)
                            phases["encode"] += time.perf_counter() - started
                            assert patch.changed_elements is not None
                            stats["changed_elements"] += patch.changed_elements
                            stats["transmitted_elements"] += patch.nnz
                            stats["payload_bytes"] += len(payload)
                            offset = writer.tell()
                            writer.write(payload)
                            entries.append(
                                TensorEntry(
                                    name,
                                    patch.dtype,
                                    patch.shape,
                                    patch.encoding,
                                    path.name,
                                    offset,
                                    len(payload),
                                    patch.nnz,
                                    checksum(payload),
                                )
                            )
                    files.append((path.name, path))
                manifest = WeightManifest(
                    version,
                    current,
                    "keyframe" if keyframe else "patch",
                    self.stream.base_model,
                    arch,
                    tuple(entries),
                    trainer_step=step,
                    training_run_id=self.training_run_id,
                )
                snapshot_id = self.stream._commit(
                    manifest, files, timings=phases, commit_stats=stats
                )
                stats["volume_version_id"] = snapshot_id
            self._previous, self._version, self._architecture = frozen, version, arch
            # Retained state advances with the committed volume even when the
            # independent metadata index is temporarily unavailable.
            started = time.perf_counter()
            try:
                self.stream._record_committed(version, snapshot_id)
            finally:
                phases["metadata"] = time.perf_counter() - started
            return version


def _state(state_dict) -> dict[str, torch.Tensor]:
    import torch

    if not isinstance(state_dict, Mapping):
        pairs = list(state_dict)
        state = dict(pairs)
        if len(state) != len(pairs):
            raise WeightStreamError("Duplicate tensor name in state dict")
    else:
        state = dict(state_dict)
    if not state or any(
        not isinstance(name, str)
        or not name
        or not isinstance(tensor, torch.Tensor)
        or tensor.layout != torch.strided
        for name, tensor in state.items()
    ):
        raise WeightStreamError("Expected a nonempty named dense tensor state dict")
    return state
