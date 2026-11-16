from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

from chalkcompute.weights.serving.managed_target import ManagedWeightTarget
from chalkcompute.weights.serving.supervisor import (
    AdmissionGate,
    UnhealthyError,
    create_app,
)


def target() -> ManagedWeightTarget:
    return ManagedWeightTarget(
        schema_version=1,
        deployment_id="deployment-1",
        revision_id="revision-1",
        target_generation=1,
        stream_id="stream-1",
        logical_version=0,
        volume_id="volume-1",
        volume_version_id=42,
        manifest_sha256="a" * 64,
        weight_schema_sha256="b" * 64,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
    )


def gate_at(value: ManagedWeightTarget) -> AdmissionGate:
    gate = AdmissionGate()
    gate.managed_target = True
    gate.version = value.logical_version
    gate.mark_applied_target(value)
    return gate


def test_applied_target_must_match_installed_version():
    gate = AdmissionGate()
    gate.managed_target = True
    gate.version = 7
    with pytest.raises(UnhealthyError, match="differs from installed weights"):
        gate.mark_applied_target(target())
    assert gate.applied_target is None
    assert not gate.ready


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("deployment_id", "another-deployment"),
        ("revision_id", "another-revision"),
        ("target_generation", 2),
        ("stream_id", "another-stream"),
        ("logical_version", 1),
        ("volume_id", "another-volume"),
        ("volume_version_id", 43),
        ("manifest_sha256", "c" * 64),
        ("weight_schema_sha256", "d" * 64),
    ],
)
def test_renewal_cannot_extend_a_different_applied_target(field, value):
    original = target()
    gate = gate_at(original)
    deadline = gate.applied_lease_deadline
    changed = replace(
        original,
        **{field: value},
        lease_expires_at=original.lease_expires_at + timedelta(seconds=30),
    )
    with pytest.raises(UnhealthyError, match="differs from the applied target"):
        gate.renew_native_lease(changed)
    assert gate.applied_target == original.observation()
    assert gate.applied_lease_deadline == deadline


def test_health_report_bounds_freshness_and_does_not_alias_applied_identity():
    value = target()
    gate = gate_at(value)
    report = gate.health_report()
    assert report["healthy"]
    assert report["applied_version"] == 0
    assert report["applied_target"] == value.observation()
    assert 0 < report["lease_remaining_seconds"] <= 60
    assert (
        datetime.fromisoformat(report["lease_expires_at"].replace("Z", "+00:00"))
        == value.lease_expires_at
    )
    report["applied_target"]["logical_version"] = 100
    assert gate.applied_target["logical_version"] == 0
    gate.version = 1
    assert not gate.health_report()["healthy"]
    assert gate.health_report()["lease_remaining_seconds"] == 0


def test_paused_and_expired_http_reports_cannot_lend_target_freshness():
    async def run():
        value = target()
        gate = gate_at(value)
        client = TestClient(TestServer(create_app(gate, "http://127.0.0.1:1")))
        await client.start_server()
        try:
            ready = await client.get("/chalk/health")
            assert ready.status == 200
            assert (await ready.json())["lease_remaining_seconds"] > 0
            await gate.pause()
            paused = await client.get("/chalk/health")
            body = await paused.json()
            assert paused.status == 503
            assert body["applied_target"] == value.observation()
            assert body["lease_remaining_seconds"] == 0
            gate.applied_lease_deadline = time.monotonic() - 1
            assert not gate.health_report()["healthy"]
            assert gate.health_report()["lease_remaining_seconds"] == 0
        finally:
            await client.close()

    asyncio.run(run())


def test_unmanaged_health_schema_is_unchanged():
    gate = AdmissionGate()
    gate.version = 0
    assert gate.health_report() == {"applied_version": 0, "healthy": True}
