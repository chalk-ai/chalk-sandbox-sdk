from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from chalkcompute._weights_codec import checksum
from chalkcompute._weights_format import (
    TensorEntry,
    WeightManifest,
    pointer_json,
    version_dir,
)
from chalkcompute.weights.layout import WeightSchema
from chalkcompute.weights.serving.managed_target import (
    ManagedTargetError,
    ManagedTargetProvider,
)
from chalkcompute.weights.serving.full_checkpoint import FullCheckpointSyncer
from chalkcompute.weights.serving.supervisor import (
    AdmissionGate,
    UnhealthyError,
    create_app,
)
from chalkcompute.weights.serving.syncer import WeightSyncer


def _put_manifest(root: Path, version: int, parent: int | None) -> WeightManifest:
    directory = root / version_dir(version)
    directory.mkdir(parents=True)
    payload = f"tensor-{version}".encode()
    (directory / "tensor").write_bytes(payload)
    manifest = WeightManifest(
        version,
        parent,
        "keyframe" if parent is None else "patch",
        "test-model",
        "a" * 64,
        (
            TensorEntry(
                "weight",
                "uint8",
                (len(payload),),
                "dense",
                "tensor",
                0,
                len(payload),
                len(payload),
                checksum(payload),
            ),
        ),
    )
    (directory / "manifest.json").write_bytes(manifest.to_json())
    return manifest


def _descriptor(
    root: Path,
    manifest: WeightManifest,
    *,
    generation: int,
    volume_version_id: int = 42,
    lease_seconds: float = 60,
) -> dict[str, object]:
    raw = manifest.to_json()
    schema_manifest = manifest
    while schema_manifest.kind != "keyframe":
        assert schema_manifest.parent_version is not None
        schema_manifest = WeightManifest.from_json(
            (
                root / version_dir(schema_manifest.parent_version) / "manifest.json"
            ).read_bytes()
        )
    return {
        "schema_version": 1,
        "deployment_id": "deployment-1",
        "revision_id": "revision-1",
        "target_generation": generation,
        "stream_id": "stream-1",
        "logical_version": manifest.version,
        "volume_id": "vol/canonical-1",
        "volume_version_id": volume_version_id,
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "weight_schema_sha256": WeightSchema.from_manifest(schema_manifest).sha256,
        "lease_expires_at": (
            datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
        )
        .isoformat()
        .replace("+00:00", "Z"),
    }


def _write_target(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _provider(path: Path) -> ManagedTargetProvider:
    return ManagedTargetProvider(
        path,
        expected_deployment_id="deployment-1",
        expected_revision_id="revision-1",
        expected_stream_id="stream-1",
        expected_volume_id="vol/canonical-1",
    )


def test_target_descriptor_is_strict_and_rejects_duplicate_keys(tmp_path):
    root = tmp_path / "weights"
    root.mkdir()
    manifest = _put_manifest(root, 0, None)
    target_path = tmp_path / "target.json"
    document = _descriptor(root, manifest, generation=1)
    _write_target(target_path, document)

    target = _provider(target_path).current()
    assert target.logical_version == 0
    assert target.volume_version_id == 42

    duplicate = json.dumps(document).replace(
        '"schema_version": 1', '"schema_version": 1, "schema_version": 1'
    )
    target_path.write_text(duplicate, encoding="utf-8")
    with pytest.raises(ManagedTargetError, match="Duplicate"):
        _provider(target_path).current()

    document["target_generation"] = 0
    _write_target(target_path, document)
    with pytest.raises(ManagedTargetError, match="target_generation"):
        _provider(target_path).current()

    document["target_generation"] = 1
    document["volume_version_id"] = 0
    _write_target(target_path, document)
    with pytest.raises(ManagedTargetError, match="volume_version_id"):
        _provider(target_path).current()

    document["volume_version_id"] = 42
    _write_target(target_path, document)
    with pytest.raises(ManagedTargetError, match="deployment_id"):
        ManagedTargetProvider(
            target_path, expected_deployment_id="other-deployment"
        ).current()


def test_full_checkpoint_syncer_rejects_direct_managed_target(tmp_path):
    with pytest.raises(ValueError, match="does not support managed targets"):
        FullCheckpointSyncer(
            tmp_path / "weights",
            _NoopAdapter(),
            AdmissionGate(),
            managed_target=_provider(tmp_path / "target.json"),
        )


def test_managed_gate_requires_applied_target_and_live_lease():
    async def run():
        gate = AdmissionGate()
        gate.managed_target = True
        gate.version = 0

        assert not gate.ready
        with pytest.raises(UnhealthyError, match="managed target is not ready"):
            async with gate.admit():
                pass

    asyncio.run(run())


def test_managed_cli_requires_expected_deployment_and_source_identity(
    monkeypatch, tmp_path
):
    from chalkcompute.weights.serving import __main__ as entrypoint

    monkeypatch.setenv("CHALK_WEIGHT_TARGET_FILE", str(tmp_path / "target.json"))
    for name in (
        "CHALK_MODEL_DEPLOYMENT_ID",
        "CHALK_MODEL_DEPLOYMENT_REVISION_ID",
        "CHALK_WEIGHT_STREAM_ID",
        "CHALK_WEIGHT_VOLUME_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="requires expected identity"):
        entrypoint._managed_target_provider(type("Args", (), {})())

    for name, value in (
        ("CHALK_MODEL_DEPLOYMENT_ID", "deployment-1"),
        ("CHALK_MODEL_DEPLOYMENT_REVISION_ID", "revision-1"),
        ("CHALK_WEIGHT_STREAM_ID", "stream-1"),
        ("CHALK_WEIGHT_VOLUME_ID", "vol/canonical-1"),
    ):
        monkeypatch.setenv(name, value)
    provider = entrypoint._managed_target_provider(type("Args", (), {})())
    assert provider is not None
    assert provider.expected["deployment_id"] == "deployment-1"


def test_provider_rejects_stale_generation_and_equal_generation_drift(tmp_path):
    root = tmp_path / "weights"
    root.mkdir()
    first = _put_manifest(root, 0, None)
    second = _put_manifest(root, 1, 0)
    target_path = tmp_path / "target.json"
    provider = _provider(target_path)
    _write_target(target_path, _descriptor(root, second, generation=2))
    provider.current()

    _write_target(target_path, _descriptor(root, first, generation=2))
    with pytest.raises(ManagedTargetError, match="drifted"):
        provider.current()

    _write_target(target_path, _descriptor(root, first, generation=1))
    with pytest.raises(ManagedTargetError, match="backwards"):
        provider.current()


def test_http_only_gate_renews_and_preserves_monotonic_lease_deadline(tmp_path):
    async def run():
        root = tmp_path / "weights"
        root.mkdir()
        manifest = _put_manifest(root, 0, None)
        target_path = tmp_path / "target.json"
        _write_target(
            target_path, _descriptor(root, manifest, generation=1, lease_seconds=0.5)
        )
        provider = _provider(target_path)
        first = provider.current()

        gate = AdmissionGate()
        gate.managed_target = True
        gate.version = 0
        gate.mark_applied_target(first)
        first_deadline = gate.applied_lease_deadline
        assert gate.ready

        _write_target(
            target_path, _descriptor(root, manifest, generation=1, lease_seconds=1.5)
        )
        renewed = provider.current()
        gate.renew_native_lease(renewed)
        renewed_deadline = gate.applied_lease_deadline
        assert renewed_deadline is not None and renewed_deadline > first_deadline
        await asyncio.sleep(0.6)
        assert gate.ready

        gate.renew_native_lease(renewed)
        assert gate.applied_lease_deadline == renewed_deadline

        # Re-observing the same absolute expiry across a new generation must
        # not rebase it onto the current wall clock.
        new_generation = replace(renewed, target_generation=2)
        gate.mark_applied_target(new_generation)
        assert gate.applied_lease_deadline == renewed_deadline

    asyncio.run(run())


def test_failed_managed_update_never_acknowledges_or_reopens(tmp_path):
    class Handle:
        def __init__(self) -> None:
            self.serving = False

        def set_serving(self, serving: bool, lease_expires_at: float | None = None):
            self.serving = serving

    async def run():
        root = tmp_path / "weights"
        root.mkdir()
        manifest = _put_manifest(root, 0, None)
        target_path = tmp_path / "target.json"
        _write_target(target_path, _descriptor(root, manifest, generation=1))
        old_target = _provider(target_path).current()
        pending_target = replace(old_target, target_generation=2)
        handle = Handle()
        gate = AdmissionGate(handle)
        gate.managed_target = True
        gate.version = 0
        gate.mark_applied_target(old_target)
        await gate.pause()

        with pytest.raises(ManagedTargetError, match="changed during no-op validation"):
            async with gate.update(1, target=pending_target):
                raise ManagedTargetError("Target changed during no-op validation")

        assert gate.paused
        assert not gate.ready
        assert not handle.serving
        assert gate.applied_target == old_target.observation()

    asyncio.run(run())


def test_managed_sync_uses_exact_descriptor_and_pauses_on_generation_change(tmp_path):
    class Adapter:
        def __init__(self) -> None:
            self.applied: list[int] = []
            self.resets = 0

        async def apply(self, root, manifest):
            self.applied.append(manifest.version)

        async def reset_cache(self):
            self.resets += 1

    class Handle:
        def __init__(self) -> None:
            self.serving = False
            self.expiry: float | None = None

        def set_serving(self, serving: bool, lease_expires_at: float | None = None):
            self.serving = serving
            if serving:
                assert lease_expires_at is not None
                self.expiry = lease_expires_at

    async def run():
        root = tmp_path / "weights"
        root.mkdir()
        initial = _put_manifest(root, 0, None)
        next_version = _put_manifest(root, 1, 0)
        # The managed target is explicit and ignores the shared PIN pointer.
        (root / "PIN").write_bytes(pointer_json(1))
        target_path = tmp_path / "target.json"
        _write_target(target_path, _descriptor(root, initial, generation=1))

        handle = Handle()
        gate = AdmissionGate(handle)
        adapter = Adapter()
        syncer = WeightSyncer(
            root,
            adapter,
            gate,
            managed_target=_provider(target_path),
            reload=lambda _: None,
        )

        async def launch(model_dir: Path):
            assert model_dir.name == version_dir(0)

        await syncer.bootstrap(launch)
        assert gate.ready
        assert gate.version == 0
        assert gate.applied_target == syncer.managed_target.current().observation()
        assert handle.serving
        assert handle.expiry is not None

        target = _descriptor(root, next_version, generation=2, volume_version_id=43)
        _write_target(target_path, target)
        syncer.managed_target.current()
        await gate.pause()
        assert not gate.ready
        assert not handle.serving

        assert await syncer.sync_once()
        assert gate.ready
        assert gate.version == 1
        assert gate.applied_target == syncer.managed_target.current().observation()
        assert adapter.applied == [1]
        assert handle.serving

        # A new generation with identical publication content only advances
        # the observation tuple; it does not rewrite engine weights.
        target = _descriptor(root, next_version, generation=3, volume_version_id=43)
        _write_target(target_path, target)
        syncer.managed_target.current()
        await gate.pause()
        assert not gate.ready
        assert not await syncer.sync_once()
        assert gate.ready
        assert gate.applied_target == syncer.managed_target.current().observation()
        assert adapter.applied == [1]

    asyncio.run(run())


def test_managed_target_lease_expiry_fails_closed_without_renewal(tmp_path):
    class Handle:
        def __init__(self) -> None:
            self.serving = False
            self.expiry: float | None = None

        def set_serving(self, serving: bool, lease_expires_at: float | None = None):
            self.serving = serving
            if serving:
                assert lease_expires_at is not None
                self.expiry = lease_expires_at

    async def run():
        root = tmp_path / "weights"
        root.mkdir()
        manifest = _put_manifest(root, 0, None)
        target_path = tmp_path / "target.json"
        _write_target(
            target_path, _descriptor(root, manifest, generation=1, lease_seconds=0.6)
        )
        provider = _provider(target_path)
        handle = Handle()
        gate = AdmissionGate(handle)
        syncer = WeightSyncer(
            root,
            _NoopAdapter(),
            gate,
            managed_target=provider,
            reload=lambda _: None,
        )

        async def launch(model_dir: Path):
            pass

        await syncer.bootstrap(launch)
        client = TestClient(TestServer(create_app(gate, "http://127.0.0.1:1")))
        await client.start_server()
        ready = await client.get("/chalk/health")
        assert ready.status == 200
        assert (await ready.json())[
            "applied_target"
        ] == provider.current().observation()
        assert handle.serving

        watcher = asyncio.create_task(syncer._watch_managed_target())
        await asyncio.wait_for(gate.failed.wait(), timeout=2)
        assert not gate.ready
        assert not handle.serving
        unavailable = await client.get("/chalk/health")
        assert unavailable.status == 503
        await watcher
        await client.close()

    asyncio.run(run())


class _NoopAdapter:
    async def apply(self, root, manifest):
        pass

    async def reset_cache(self):
        pass


def test_cross_language_target_fingerprint_vectors():
    from chalkcompute.weights.serving.managed_target import target_fingerprint

    vectors = json.loads(
        (Path(__file__).parent / "fixtures/model_target_fingerprints.json").read_text()
    )
    names = (
        "deployment_id",
        "revision_id",
        "target_generation",
        "stream_id",
        "logical_version",
        "volume_id",
        "volume_version_id",
        "manifest_sha256",
        "weight_schema_sha256",
    )
    for vector in vectors:
        assert (
            target_fingerprint(dict(zip(names, vector["fields"])))
            == vector["fingerprint"]
        )
