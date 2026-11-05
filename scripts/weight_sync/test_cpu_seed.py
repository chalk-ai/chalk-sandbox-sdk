"""CPU lease and ownership guards; no infrastructure calls."""

import json
import os
import time
from types import SimpleNamespace

import pytest

from cpu_seed import GuardedSandboxStub, valid_handoff, write_json


def handoff():
    record = {
        "status": "paused_proof",
        "cpu_phase_id": "cpu-unique",
        "proof_sha256": "proof",
        "name": "owned-volume",
        "snapshot_id": "immutable",
        "hf_revision": "hf-commit",
        "prepared_image_sha256": "image",
        "source_overlay_sha256": "overlay",
        "training_deploy_image_uri": "trainer",
        "cpu_deadline_epoch": time.time() + 60,
    }
    authorization = {
        key: record[key]
        for key in (
            "cpu_phase_id",
            "proof_sha256",
            "snapshot_id",
            "hf_revision",
            "prepared_image_sha256",
            "source_overlay_sha256",
            "training_deploy_image_uri",
        )
    }
    authorization.update(
        volume_name=record["name"],
        owns_transferred_source_volume=True,
        action="transfer_data_ownership",
        gpu_phase_id="gpu-unique",
        gpu_guard_ready=True,
        gpu_deadline_epoch=time.time() + 60,
        approved_by="parent",
    )
    return record, authorization


@pytest.mark.parametrize(
    "field",
    [
        "cpu_phase_id",
        "proof_sha256",
        "volume_name",
        "snapshot_id",
        "hf_revision",
        "prepared_image_sha256",
        "source_overlay_sha256",
        "training_deploy_image_uri",
        "owns_transferred_source_volume",
        "action",
        "gpu_phase_id",
        "gpu_guard_ready",
        "gpu_deadline_epoch",
        "approved_by",
    ],
)
def test_handoff_rejects_missing_or_changed_provenance(field):
    record, authorization = handoff()
    assert valid_handoff(record, authorization)
    del authorization[field]
    assert not valid_handoff(record, authorization)


def test_handoff_rejects_expired_cpu_lease_and_preproof_state():
    record, authorization = handoff()
    record["cpu_deadline_epoch"] = time.time() - 1
    assert not valid_handoff(record, authorization)
    record["cpu_deadline_epoch"] = time.time() + 60
    record["status"] = "seeding"
    assert not valid_handoff(record, authorization)


def test_create_requires_independent_live_watchdog_and_persists_id(tmp_path):
    record, _ = handoff()
    ledger = tmp_path / "launcher.json"
    calls = []

    class Stub:
        def create_sandbox(self, request, **kwargs):
            persisted = json.loads(ledger.read_text())
            assert persisted["submission_inflight"] is True
            calls.append(request)
            return SimpleNamespace(sandbox=SimpleNamespace(id="sandbox-exact"))

    stub = GuardedSandboxStub(Stub(), record, ledger)
    with pytest.raises(FileNotFoundError):
        stub.create_sandbox("request")
    assert not calls
    write_json(
        tmp_path / "cpu-watchdog-ready.json",
        {"phase_id": "other-phase", "pid": os.getpid()},
    )
    with pytest.raises(RuntimeError, match="phase mismatch"):
        stub.create_sandbox("request")
    assert not calls
    write_json(
        tmp_path / "cpu-watchdog-ready.json",
        {"phase_id": record["cpu_phase_id"], "pid": os.getpid()},
    )
    stub.create_sandbox("request")
    assert calls == ["request"]
    persisted = json.loads(ledger.read_text())
    assert persisted["seed_sandbox_id"] == "sandbox-exact"
    assert persisted["submission_inflight"] is False
    record["cpu_deadline_epoch"] = time.time() - 1
    with pytest.raises(TimeoutError):
        stub.create_sandbox("second")
    assert calls == ["request"]


def test_ambiguous_create_remains_inflight_for_detached_recovery(tmp_path):
    record, _ = handoff()
    ledger = tmp_path / "launcher.json"
    write_json(
        tmp_path / "cpu-watchdog-ready.json",
        {"phase_id": record["cpu_phase_id"], "pid": os.getpid()},
    )

    class Stub:
        def create_sandbox(self, request, **kwargs):
            raise ConnectionError("lost create response")

    with pytest.raises(ConnectionError):
        GuardedSandboxStub(Stub(), record, ledger).create_sandbox("request")
    assert json.loads(ledger.read_text())["submission_inflight"] is True
