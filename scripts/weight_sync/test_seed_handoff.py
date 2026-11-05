"""CPU proof admission guards, independent of infrastructure."""

import hashlib
import json
import time

import pytest

from seed_handoff import load_seed_lease


def fixture(tmp_path):
    image = tmp_path / "image.json"
    image.write_text('{"image_uri":"frozen"}')
    proof = {
        "snapshot_id": 42,
        "volume_name": "owned",
        "revision": "hf-exact",
        "model": "Qwen/Qwen3-8B",
        "current": 0,
        "files": {"part.safetensors": {"bytes": 16}},
        "tensor_count": 1,
        "elements": 8,
    }
    proof_path = tmp_path / "cpu-proof.json"
    proof_path.write_text(json.dumps(proof))
    record = {
        "status": "paused_proof",
        "cpu_deadline_epoch": time.time() + 60,
        "cpu_compute_released": True,
        "seed_resources_verified_absent": True,
        "proof_sha256": hashlib.sha256(proof_path.read_bytes()).hexdigest(),
        "prepared_image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        "snapshot_id": 42,
        "name": "owned",
        "hf_revision": "hf-exact",
        "hf_model": "Qwen/Qwen3-8B",
    }
    ledger = tmp_path / "launcher.json"
    ledger.write_text(json.dumps(record))
    return ledger, image, record, proof_path


def test_complete_immutable_cpu_proof_admitted(tmp_path):
    ledger, image, record, _ = fixture(tmp_path)
    loaded, proof = load_seed_lease(ledger, image)
    assert loaded == record
    assert proof["snapshot_id"] == 42


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "seeding"),
        ("cpu_deadline_epoch", 0),
        ("cpu_compute_released", False),
        ("seed_resources_verified_absent", False),
        ("proof_sha256", "changed"),
        ("prepared_image_sha256", "changed"),
        ("snapshot_id", 43),
        ("name", "other-volume"),
        ("hf_revision", "other-revision"),
        ("hf_model", "other-model"),
    ],
)
def test_incomplete_or_wrong_cpu_proof_refused(tmp_path, field, value):
    ledger, image, record, _ = fixture(tmp_path)
    record[field] = value
    ledger.write_text(json.dumps(record))
    with pytest.raises(RuntimeError):
        load_seed_lease(ledger, image)


def test_changed_proof_bytes_refused(tmp_path):
    ledger, image, _, proof_path = fixture(tmp_path)
    proof_path.write_text(proof_path.read_text() + " ")
    with pytest.raises(RuntimeError):
        load_seed_lease(ledger, image)


@pytest.mark.parametrize("wrong_exit_phase", [False, True])
def test_transfer_waits_for_bound_guard_exit_before_metadata_read(
    tmp_path, monkeypatch, wrong_exit_phase
):
    from types import SimpleNamespace
    import chalkcompute as cc
    import seed_handoff
    from chalkcompute._weights_format import pointer_json

    ledger, image, record, proof_path = fixture(tmp_path)
    manifest = b"proof-bound-manifest"
    proof = json.loads(proof_path.read_text())
    proof["manifest_sha256"] = hashlib.sha256(manifest).hexdigest()
    proof_path.write_text(json.dumps(proof))
    record.update(
        cpu_phase_id="cpu-unique",
        source_overlay_sha256="overlay",
        training_deploy_image_uri="trainer",
        proof_sha256=hashlib.sha256(proof_path.read_bytes()).hexdigest(),
    )
    ledger.write_text(json.dumps(record))
    gpu_out = tmp_path / "gpu"
    gpu_out.mkdir()
    metadata_reads = []

    class Guard:
        out = gpu_out
        record = {
            "gpu_phase_id": "gpu-unique",
            "gpu_deadline_epoch": time.time() + 120,
            "owns_source_volume": False,
            "execution_config": {"budget_seconds": 120},
            "execution_config_sha256": "bounded-plan",
        }

        def save(self):
            pass

        def check_budget(self):
            pass

    guard = Guard()
    original_write = seed_handoff.write_json

    def simulate_parent_and_cpu_exit(path, data):
        original_write(path, data)
        assert guard.record["owns_source_volume"] is True
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
            action="transfer_data_ownership",
            owns_transferred_source_volume=True,
            gpu_phase_id=guard.record["gpu_phase_id"],
            gpu_guard_ready=True,
            gpu_deadline_epoch=guard.record["gpu_deadline_epoch"],
            approved_by="parent",
            execution_config_sha256=guard.record["execution_config_sha256"],
        )
        original_write(tmp_path / "cpu-authorization.json", authorization)
        original_write(
            tmp_path / "cpu-watchdog-exited.json",
            {
                "phase_id": "wrong" if wrong_exit_phase else record["cpu_phase_id"],
                "transferred": True,
            },
        )
        original_write(
            ledger,
            {
                **record,
                "status": "data_ownership_transferred",
                "gpu_phase_id": guard.record["gpu_phase_id"],
            },
        )

    class Volume:
        def __init__(self, name, create_if_missing):
            assert name == record["name"] and create_if_missing is False
            metadata_reads.append("opened")
            self.version = SimpleNamespace(id=proof["snapshot_id"])

        def reload(self):
            pass

        def at_version(self, version):
            assert version == proof["snapshot_id"]
            return self

        def read_file(self, path):
            metadata_reads.append(path)
            return pointer_json(0) if path == "CURRENT" else manifest

    monkeypatch.setattr(seed_handoff, "write_json", simulate_parent_and_cpu_exit)
    monkeypatch.setattr(cc, "Volume", Volume)
    if wrong_exit_phase:
        with pytest.raises(RuntimeError, match="does not bind"):
            seed_handoff.await_seed_transfer(guard, ledger, record, proof, image)
        assert not metadata_reads
    else:
        seed_handoff.await_seed_transfer(guard, ledger, record, proof, image)
        assert metadata_reads == ["opened", "CURRENT", "v000000/manifest.json"]
        assert guard.record["status"] == "cpu_seed_transfer_verified"


def test_two_receivers_cannot_share_or_steal_one_cpu_lease(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from seed_handoff import reserve_cpu_receiver

    ledger = tmp_path / "launcher.json"
    ledger.write_text("{}")

    def claim(phase):
        record = {"cpu_seed_phase_id": "cpu-exact", "gpu_phase_id": phase}
        try:
            return reserve_cpu_receiver(ledger, record, tmp_path / phase)
        except RuntimeError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ["receiver-a", "receiver-b"]))
    assert sum(item is not None for item in results) == 1
    winner = next(item for item in results if item is not None)
    assert (
        reserve_cpu_receiver(
            ledger,
            {"cpu_seed_phase_id": "cpu-exact", "gpu_phase_id": winner["gpu_phase_id"]},
            tmp_path / winner["gpu_phase_id"],
        )
        == winner
    )
    with pytest.raises(RuntimeError, match="refusing takeover"):
        reserve_cpu_receiver(
            ledger,
            {"cpu_seed_phase_id": "cpu-exact", "gpu_phase_id": "stale-retry"},
            tmp_path / "new",
        )
