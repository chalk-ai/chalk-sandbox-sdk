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
        reply = handle.read().decode("utf-8", errors="replace")
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
        self.architecture_hash: str | None = None
        self._lock = asyncio.Lock()

    def desired(self) -> int:
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
            plan = await asyncio.to_thread(self.plan, self.desired(), bootstrap=True)
            await asyncio.to_thread(self.verify, plan)
            first, *patches = plan.manifests
            if first.kind != "keyframe":
                raise StreamError("Boot requires a keyframe")
            self.architecture_hash = first.architecture_hash
            try:
                await launch(self.root / version_dir(first.version))
                for manifest in patches:
                    await self.adapter.apply(
                        self.root / version_dir(manifest.version), manifest
                    )
                if patches:
                    await self.adapter.reset_cache()
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
            plan = await asyncio.to_thread(self.plan, target)
            if not plan.manifests:
                return False
            await asyncio.to_thread(self.verify, plan)
            pause_start = time.perf_counter()
            async with self.gate.update(self.drain_timeout):
                apply_start = time.perf_counter()
                try:
                    for manifest in plan.manifests:
                        await self.adapter.apply(
                            self.root / version_dir(manifest.version), manifest
                        )
                    await self.adapter.reset_cache()
                except BaseException as exc:
                    await self.gate.fail()
                    self.metrics.failures += 1
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise ApplyError(
                        "Weight apply failed; replica must restart"
                    ) from exc
                self.gate.version = target
                self.architecture_hash = plan.manifests[-1].architecture_hash
                self.metrics.apply_ms = (time.perf_counter() - apply_start) * 1000
                self.metrics.pause_ms = (time.perf_counter() - pause_start) * 1000
                self.metrics.bytes = sum(
                    e.length for m in plan.manifests for e in m.tensors
                )
                total = sum(
                    math.prod(e.shape) for m in plan.manifests for e in m.tensors
                )
                changed = sum(e.nnz for m in plan.manifests for e in m.tensors)
                self.metrics.density = changed / total if total else 0
                self.metrics.lag_versions = 0
                self.metrics.updates += 1
            return True

    async def run(self) -> None:
        while self.gate.healthy:
            try:
                await self.sync_once()
            except (StreamError, WeightStreamError, ValueError, OSError, DrainTimeout):
                # A bad publication/reload or long generation leaves the current
                # engine untouched. Keep serving it and retry next poll.
                logger.exception("Weight update deferred")
                self.metrics.failures += 1
            await asyncio.sleep(self.poll_seconds)
        raise ApplyError("Serving became unhealthy; replica must restart")
