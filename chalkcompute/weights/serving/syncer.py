from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from chalkcompute._weights_codec import verify_payloads
from chalkcompute._weights_format import (
    WeightManifest,
    WeightStreamError,
    read_pointer,
    version_dir,
)
from chalkcompute.weights.serving.adapters.base import EngineAdapter
from chalkcompute.weights.serving.managed_target import (
    ManagedTargetProvider,
    verify_target_manifest,
)
from chalkcompute.weights.serving.supervisor import AdmissionGate, DrainTimeout, Metrics

logger = logging.getLogger(__name__)


class StreamError(ValueError):
    """Desired state is invalid; no engine writes have happened."""


class ApplyError(RuntimeError):
    """Engine state might be partial; terminate and restart from a keyframe."""


@dataclass(frozen=True)
class ApplyPlan:
    target: int
    manifests: tuple[WeightManifest, ...]


def reload_mount(root: Path) -> None:
    """Use the ChalkFS control file's same-handle request/reply protocol."""
    ctl = root / ".chalk-volume" / "ctl"
    if not ctl.exists():
        return  # Ordinary local directories support the identical stream format.
    with ctl.open("r+b", buffering=0) as handle:
        handle.write(b"reload\n")
        reply = handle.read(4096).decode("utf-8", errors="replace")
    status, _, message = reply.partition("\n")
    if status != "ok":
        raise StreamError(f"ChalkFS reload failed: {message.strip()}")


class WeightSyncer:
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
        managed_target: ManagedTargetProvider | None = None,
    ) -> None:
        if poll_seconds <= 0 or drain_timeout <= 0 or keyframe_lag < 1:
            raise ValueError(
                "Sync intervals/timeouts and keyframe lag must be positive"
            )
        self.root = root.resolve()
        self.adapter = adapter
        self.gate = gate
        self.poll_seconds = poll_seconds
        self.drain_timeout = drain_timeout
        self.keyframe_lag = keyframe_lag
        self.metrics = metrics or Metrics()
        self.reload = reload
        self.managed_target = managed_target
        self.gate.managed_target = managed_target is not None
        self.architecture_hash: str | None = None
        self._model_elements: int | None = None
        self._lock = asyncio.Lock()

    def desired(self) -> int:
        if self.managed_target is not None:
            return self.managed_target.current().logical_version
        pin = self.root / "PIN"
        target = read_pointer(pin.read_bytes()) if pin.exists() else None
        if target is None:
            target = read_pointer((self.root / "CURRENT").read_bytes())
        if target is None:
            raise StreamError("The weight stream has no published version")
        return target

    def manifest(self, version: int) -> WeightManifest:
        manifest = WeightManifest.from_json(
            (self.root / version_dir(version) / "manifest.json").read_bytes()
        )
        if manifest.version != version:
            raise StreamError("Manifest version differs from its directory")
        if (
            self.architecture_hash
            and manifest.architecture_hash != self.architecture_hash
        ):
            raise StreamError(
                "Model architecture changed; deploy a new serving replica"
            )
        return manifest

    def plan(self, target: int, *, bootstrap: bool = False) -> ApplyPlan:
        """Walk backwards from desired state, never selecting a keyframe after PIN."""
        current = None if bootstrap else self.gate.version
        if current == target:
            return ApplyPlan(target, ())
        use_current = current is not None and 0 < target - current <= self.keyframe_lag
        chain: list[WeightManifest] = []
        cursor = target
        seen: set[int] = set()
        while True:
            if use_current and cursor == current:
                break
            if cursor in seen:
                raise StreamError("Weight stream parent cycle")
            seen.add(cursor)
            try:
                manifest = self.manifest(cursor)
            except FileNotFoundError as exc:
                raise StreamError(f"Missing manifest for version {cursor}") from exc
            chain.append(manifest)
            if manifest.kind == "keyframe":
                break
            parent = manifest.parent_version
            if parent is None or parent >= cursor or parent < 0:
                raise StreamError("Patch parent must be an earlier version")
            cursor = parent
        chain.reverse()
        architecture = chain[0].architecture_hash if chain else self.architecture_hash
        if any(m.architecture_hash != architecture for m in chain):
            raise StreamError("Version chain changes model architecture")
        # Long lag and backward PIN require a complete reset, not forward patches.
        if not use_current and chain and chain[0].kind != "keyframe":
            raise StreamError("No keyframe can materialize the desired version")
        return ApplyPlan(target, tuple(chain))

    def verify(self, plan: ApplyPlan) -> None:
        # Verify the *entire* replay before the first GPU write. Corruption in a
        # later version cannot leave the engine partially advanced.
        for manifest in plan.manifests:
            verify_payloads(self.root / version_dir(manifest.version), manifest)

    async def bootstrap(self, launch: Callable[[Path], Awaitable[None]]) -> None:
        async with self._lock:
            await asyncio.to_thread(self.reload, self.root)
            managed_target_provider = self.managed_target
            target = (
                await asyncio.to_thread(managed_target_provider.current)
                if managed_target_provider is not None
                else None
            )
            desired = target.logical_version if target is not None else self.desired()
            plan = await asyncio.to_thread(self.plan, desired, bootstrap=True)
            if target is not None:
                await asyncio.to_thread(verify_target_manifest, self.root, target)
            await asyncio.to_thread(self.verify, plan)
            if target is not None:
                assert managed_target_provider is not None
                target = await asyncio.to_thread(
                    managed_target_provider.assert_current, target
                )
            first, *patches = plan.manifests
            if first.kind != "keyframe":
                raise StreamError("Boot requires a keyframe")
            self.architecture_hash = first.architecture_hash
            self._model_elements = sum(math.prod(e.shape) for e in first.tensors)
            try:
                await launch(self.root / version_dir(first.version))
                initialize = getattr(self.adapter, "initialize", None)
                if initialize is not None:
                    await initialize(self.root / version_dir(first.version), first)
                for manifest in patches:
                    await self.adapter.apply(
                        self.root / version_dir(manifest.version), manifest
                    )
                if patches:
                    await self.adapter.reset_cache()
                if target is not None:
                    assert managed_target_provider is not None
                    target = await asyncio.to_thread(
                        managed_target_provider.assert_current, target
                    )
                self.gate.version = plan.target
                if target is not None:
                    self.gate.mark_applied_target(target)
            except BaseException:
                await self.gate.fail()
                raise

    async def sync_once(self) -> bool:
        async with self._lock:
            await asyncio.to_thread(self.reload, self.root)
            managed_target_provider = self.managed_target
            managed_target = (
                await asyncio.to_thread(managed_target_provider.current)
                if managed_target_provider is not None
                else None
            )
            target = (
                managed_target.logical_version
                if managed_target is not None
                else self.desired()
            )
            current = self.gate.version
            self.metrics.lag_versions = (
                max(0, target - current) if current is not None else target
            )
            plan = await asyncio.to_thread(self.plan, target)
            if managed_target is not None:
                await asyncio.to_thread(
                    verify_target_manifest, self.root, managed_target
                )
                previous_applied = self.gate.applied_target
                if current == target and previous_applied is not None:
                    expected_content = managed_target.observation()
                    previous_content = previous_applied.copy()
                    expected_content.pop("target_generation")
                    previous_content.pop("target_generation")
                    if expected_content != previous_content:
                        raise ValueError(
                            "A logical weight version changed content or volume provenance"
                        )
            if not plan.manifests:
                if managed_target is not None:
                    assert managed_target_provider is not None
                    managed_target = await asyncio.to_thread(
                        managed_target_provider.assert_current, managed_target
                    )
                    if self.gate.applied_target is None:
                        raise ValueError("Managed target has not been applied")
                    next_observation = managed_target.observation()
                    if (
                        next_observation != self.gate.applied_target
                        or self.gate.paused
                    ):
                        async with self.gate.update(
                            self.drain_timeout, target=managed_target
                        ):
                            latest_target = await asyncio.to_thread(
                                managed_target_provider.assert_current, managed_target
                            )
                            self.gate.mark_applied_target(latest_target)
                    else:
                        self.gate.renew_native_lease(managed_target)
                return False
            await asyncio.to_thread(self.verify, plan)
            if managed_target is not None:
                assert managed_target_provider is not None
                managed_target = await asyncio.to_thread(
                    managed_target_provider.assert_current, managed_target
                )
            pause_start = time.perf_counter()
            async with self.gate.update(
                self.drain_timeout, target=managed_target
            ):
                apply_start = time.perf_counter()
                try:
                    if managed_target is not None:
                        assert managed_target_provider is not None
                        managed_target = await asyncio.to_thread(
                            managed_target_provider.assert_current, managed_target
                        )
                    for manifest in plan.manifests:
                        await self.adapter.apply(
                            self.root / version_dir(manifest.version), manifest
                        )
                    await self.adapter.reset_cache()
                    if managed_target is not None:
                        assert managed_target_provider is not None
                        managed_target = await asyncio.to_thread(
                            managed_target_provider.assert_current, managed_target
                        )
                except BaseException as exc:
                    await self.gate.fail()
                    self.metrics.failures += 1
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise ApplyError(
                        "Weight apply failed; replica must restart"
                    ) from exc
                self.gate.version = target
                if managed_target is not None:
                    self.gate.mark_applied_target(managed_target)
                self.architecture_hash = plan.manifests[-1].architecture_hash
                self.metrics.apply_ms = (time.perf_counter() - apply_start) * 1000
                self.metrics.pause_ms = (time.perf_counter() - pause_start) * 1000
                payloads = {
                    (m.version, e.file, e.offset, e.length)
                    for m in plan.manifests
                    for e in m.tensors
                }
                self.metrics.bytes = sum(length for _, _, _, length in payloads)
                for manifest in plan.manifests:
                    if manifest.kind == "keyframe":
                        self._model_elements = sum(
                            math.prod(e.shape) for e in manifest.tensors
                        )
                # Density describes the target publication against the whole
                # model, including untouched tensors. Bytes/time describe the
                # entire replay, whose intermediate changes may overlap.
                transmitted = sum(e.nnz for e in plan.manifests[-1].tensors)
                self.metrics.density = (
                    transmitted / self._model_elements if self._model_elements else 0
                )
                self.metrics.lag_versions = 0
                self.metrics.updates += 1
            return True

    async def run(self) -> None:
        watcher = (
            asyncio.create_task(self._watch_managed_target())
            if self.managed_target is not None
            else None
        )
        try:
            while self.gate.healthy:
                try:
                    await self.sync_once()
                except (
                    StreamError,
                    WeightStreamError,
                    ValueError,
                    OSError,
                    DrainTimeout,
                ) as exc:
                    self.metrics.failures += 1
                    if self.managed_target is not None:
                        await self.gate.fail(f"Managed target delivery failed: {exc}")
                        break
                    # Legacy mode keeps serving the current engine and retries.
                    logger.exception("Weight update deferred")
                await asyncio.sleep(self.poll_seconds)
        finally:
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
        raise ApplyError("Serving became unhealthy; replica must restart")

    async def _watch_managed_target(self) -> None:
        assert self.managed_target is not None
        while self.gate.healthy:
            await asyncio.sleep(min(0.25, self.poll_seconds))
            try:
                target = await asyncio.to_thread(self.managed_target.current)
                applied = self.gate.applied_target
                if applied is not None:
                    if target.target_generation != applied.get("target_generation"):
                        if not self.gate.paused:
                            await self.gate.pause()
                    else:
                        self.gate.renew_native_lease(target)
            except Exception as exc:
                self.metrics.failures += 1
                await self.gate.fail(f"Managed target lease or delivery failed: {exc}")
                return
