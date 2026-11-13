"""Mounted-stream replay for experimental engines requiring a full checkpoint."""

from __future__ import annotations

import asyncio
import json
import math
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from chalkcompute._weights_codec import apply_patch, read_patch, verify_payloads
from chalkcompute._weights_format import (
    WeightStreamError,
    architecture_hash,
    safe_path,
    version_dir,
)
from chalkcompute.weights.serving.adapters.base import EngineAdapter
from chalkcompute.weights.serving.adapters.sglang import validate_config
from chalkcompute.weights.serving.managed_target import TargetProvider
from chalkcompute.weights.serving.syncer import (
    ApplyError,
    ApplyPlan,
    StreamError,
    WeightSyncer,
    reload_mount,
)
from chalkcompute.weights.serving.supervisor import AdmissionGate, Metrics


def materialize(root: Path, plan: ApplyPlan, output: Path) -> None:
    """Write one complete final checkpoint; retain no CPU model between calls.

    The caller owns output and must not expose it until this function returns.
    Payloads are verified here, on the same immutable mounted version paths used
    for replay. No Volume client, upload or mutable CURRENT read is involved.
    """
    from safetensors.torch import load_file, save_file

    if (
        not plan.manifests
        or plan.manifests[0].kind != "keyframe"
        or plan.manifests[-1].version != plan.target
    ):
        raise StreamError("Full checkpoint requires a complete keyframe-to-target plan")
    first = plan.manifests[0]
    source = root / version_dir(first.version)
    config = (source / "config.json").read_bytes()
    validate_config(config)
    entries = {entry.name: entry for entry in first.tensors}
    if not entries or any(
        entry.dtype != "bfloat16" or entry.offset != 0 for entry in entries.values()
    ):
        raise WeightStreamError("Full checkpoint requires dense BF16 safetensors")
    state = {}
    for manifest in plan.manifests:
        verify_payloads(root / version_dir(manifest.version), manifest)
    for filename in sorted({entry.file for entry in first.tensors}):
        if not filename.endswith(".safetensors"):
            raise WeightStreamError("Keyframe must contain safetensors shards")
        shard = load_file(str(safe_path(source, filename)), device="cpu")
        for name, tensor in shard.items():
            entry = entries.get(name)
            if (
                name in state
                or entry is None
                or entry.file != filename
                or tuple(tensor.shape) != entry.shape
                or str(tensor.dtype) != "torch.bfloat16"
            ):
                raise WeightStreamError("Keyframe tensor catalog differs from manifest")
            state[name] = tensor.clone().contiguous()
        del shard
    if (
        set(state) != set(entries)
        or architecture_hash(config, state.items()) != first.architecture_hash
    ):
        raise WeightStreamError(
            "Keyframe config or tensor architecture differs from manifest"
        )
    previous = first.version
    for manifest in plan.manifests[1:]:
        if (
            manifest.kind != "patch"
            or manifest.parent_version != previous
            or manifest.architecture_hash != first.architecture_hash
        ):
            raise WeightStreamError(
                "Full checkpoint plan is not a contiguous compatible replay"
            )
        for entry in manifest.tensors:
            if entry.name not in state:
                raise WeightStreamError("Patch names an unknown checkpoint tensor")
            patch = read_patch(root / version_dir(manifest.version), entry)
            apply_patch(state[entry.name], patch)
            del patch
        previous = manifest.version
    # Copy only tokenizer/config assets, omitting old shard indices and payloads.
    for path in source.rglob("*"):
        relative = path.relative_to(source).as_posix()
        if (
            path.is_file()
            and path.suffix in (".json", ".model", ".tiktoken", ".txt")
            and path.name
            not in (
                "manifest.json",
                "model.safetensors.index.json",
                "pytorch_model.bin.index.json",
            )
        ):
            path = safe_path(source, relative)
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
    save_file(state, str(output / "model.safetensors"), metadata={"format": "pt"})
    (output / "chalk-weight-checkpoint.json").write_text(
        json.dumps(
            {"version": plan.target, "architecture_hash": first.architecture_hash}
        )
    )
    # state and all tensor references die before returning to the admission gate.


class FullCheckpointSyncer(WeightSyncer):
    """Dense reload baseline; the existing incremental vLLM path is unchanged."""

    _checkpoint: tempfile.TemporaryDirectory | None = None
    _bootstrap_checkpoint: tempfile.TemporaryDirectory | None = None
    _uncertain_checkpoint: tempfile.TemporaryDirectory | None = None

    def __init__(
        self,
        root: Path,
        adapter: EngineAdapter,
        gate: AdmissionGate,
        *,
        poll_seconds: float = 2,
        drain_timeout: float = 30,
        keyframe_lag: int = 50,
        metrics: Metrics | None = None,
        reload: Callable[[Path], None] = reload_mount,
        managed_target: TargetProvider | None = None,
    ) -> None:
        if managed_target is not None:
            raise ValueError("Full checkpoint syncer does not support managed targets")
        super().__init__(
            root,
            adapter,
            gate,
            poll_seconds=poll_seconds,
            drain_timeout=drain_timeout,
            keyframe_lag=keyframe_lag,
            metrics=metrics,
            reload=reload,
        )

    async def _prepare(self, plan: ApplyPlan) -> tempfile.TemporaryDirectory:
        directory = tempfile.TemporaryDirectory(prefix="chalk-weight-checkpoint-")
        task = asyncio.create_task(
            asyncio.to_thread(materialize, self.root, plan, Path(directory.name))
        )
        try:
            await asyncio.shield(task)
            return directory
        except BaseException:
            # Cancelling to_thread does not stop its writer. Join it before
            # deleting output so the background writer cannot leak/recreate it.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue  # Repeated cancellation must not detach the writer.
                except BaseException:
                    break
            if not task.cancelled():
                task.exception()  # Consume a writer error while preserving cancellation.
            directory.cleanup()
            raise

    def close(self) -> None:
        """Call only after stopping the engine, which may read tokenizer assets."""
        for directory in (
            self._checkpoint,
            self._bootstrap_checkpoint,
            self._uncertain_checkpoint,
        ):
            if directory is not None:
                directory.cleanup()
        self._checkpoint = self._bootstrap_checkpoint = self._uncertain_checkpoint = (
            None
        )

    def _install(self, directory: tempfile.TemporaryDirectory) -> None:
        old, self._checkpoint = self._checkpoint, directory
        if old is not None and old is not self._bootstrap_checkpoint:
            old.cleanup()

    async def bootstrap(self, launch, initial_model_dir: Path | None = None) -> None:
        if initial_model_dir is not None:
            raise ValueError(
                "Full-checkpoint runtime does not support snapshot bootstrap"
            )
        async with self._lock:
            await asyncio.to_thread(self.reload, self.root)
            plan = await asyncio.to_thread(self.plan, self.desired(), bootstrap=True)
            directory = await self._prepare(plan)
            # Keep complete assets for the engine's lifetime, but no CPU state.
            self._install(directory)
            self._bootstrap_checkpoint = directory
            try:
                await launch(Path(directory.name))
                self.architecture_hash = plan.manifests[0].architecture_hash
                self._model_elements = sum(
                    math.prod(entry.shape) for entry in plan.manifests[0].tensors
                )
                self.gate.version = plan.target
            except BaseException:
                await self.gate.fail()
                raise

    async def sync_once(self) -> bool:
        async with self._lock:
            await asyncio.to_thread(self.reload, self.root)
            target = self.desired()
            current = self.gate.version
            self.metrics.lag_versions = (
                max(0, target - current) if current is not None else target
            )
            if target == current:
                return False
            plan = await asyncio.to_thread(self.plan, target, bootstrap=True)
            directory = await self._prepare(plan)
            pause_start = time.perf_counter()
            try:
                async with self.gate.update(self.drain_timeout):
                    apply_start = time.perf_counter()
                    try:
                        await self.adapter.apply(
                            Path(directory.name), plan.manifests[-1]
                        )
                        self._install(directory)
                        directory = None
                        self.gate.version = target
                        self.architecture_hash = plan.manifests[-1].architecture_hash
                        self._model_elements = sum(
                            math.prod(entry.shape)
                            for entry in plan.manifests[0].tensors
                        )
                        self.metrics.apply_ms = (
                            time.perf_counter() - apply_start
                        ) * 1000
                        self.metrics.pause_ms = (
                            time.perf_counter() - pause_start
                        ) * 1000
                        assert self._checkpoint is not None
                        self.metrics.bytes = (
                            (Path(self._checkpoint.name) / "model.safetensors")
                            .stat()
                            .st_size
                        )
                        self.metrics.density = 1
                        self.metrics.lag_versions = 0
                        self.metrics.updates += 1
                    except BaseException as exc:
                        # A lost response/cancelled HTTP call may leave the
                        # native loader reading this path until group shutdown.
                        self._uncertain_checkpoint, directory = directory, None
                        await self.gate.fail()
                        self.metrics.failures += 1
                        if isinstance(exc, asyncio.CancelledError):
                            raise
                        raise ApplyError(
                            "Dense weight reload failed; replica must restart"
                        ) from exc
            finally:
                if directory is not None:
                    directory.cleanup()
            return True
