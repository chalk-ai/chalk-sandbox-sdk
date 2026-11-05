"""Fail-closed receiver for a CPU-proven stream; this module claims no compute."""

from __future__ import annotations

import hashlib
import json
import time

from cpu_seed import valid_handoff, write_json


def load_seed_lease(ledger, prepared_image):
    record = json.loads(ledger.read_text())
    proof_path = ledger.parent / "cpu-proof.json"
    proof_bytes = proof_path.read_bytes()
    proof = json.loads(proof_bytes)
    if not (
        record["status"] == "paused_proof"
        and record["cpu_deadline_epoch"] > time.time()
        and record.get("cpu_compute_released") is True
        and record.get("seed_resources_verified_absent") is True
        and hashlib.sha256(proof_bytes).hexdigest() == record["proof_sha256"]
        and hashlib.sha256(prepared_image.read_bytes()).hexdigest()
        == record["prepared_image_sha256"]
        and proof["snapshot_id"] == record["snapshot_id"]
        and proof["volume_name"] == record["name"]
        and proof["revision"] == record["hf_revision"]
        and proof["model"] == record["hf_model"]
        and proof["current"] == 0
        and proof["files"]
        and proof["tensor_count"] > 0
        and proof["elements"] > 0
    ):
        raise RuntimeError(
            "CPU seed lease/proof is incomplete, expired, or inconsistent"
        )
    return record, proof


def await_seed_transfer(guard, seed_ledger, seed_record, proof, prepared_image):
    """Expose guard readiness, then require parent approval and CPU guard exit."""
    # Revalidate after all CPU preparation. Overlap cleanup ownership before the
    # CPU guard exits so launcher death cannot leave a data-lease coverage gap.
    load_seed_lease(seed_ledger, prepared_image)
    guard.record["owns_source_volume"] = True
    guard.record["status"] = "awaiting_parent_gpu_authorization"
    guard.save()
    write_json(
        guard.out / "gpu-handoff-ready.json",
        {
            "gpu_phase_id": guard.record["gpu_phase_id"],
            "gpu_deadline_epoch": guard.record["gpu_deadline_epoch"],
            "gpu_guard_ready": True,
            "cpu_phase_id": seed_record["cpu_phase_id"],
            "volume_name": seed_record["name"],
            "snapshot_id": proof["snapshot_id"],
            "proof_sha256": seed_record["proof_sha256"],
            "owns_transferred_source_volume": True,
        },
    )
    while time.time() < seed_record["cpu_deadline_epoch"]:
        guard.check_budget()
        authorization_path = seed_ledger.parent / "cpu-authorization.json"
        exited_path = seed_ledger.parent / "cpu-watchdog-exited.json"
        if authorization_path.exists() and exited_path.exists():
            authorization = json.loads(authorization_path.read_text())
            exited = json.loads(exited_path.read_text())
            latest = json.loads(seed_ledger.read_text())
            if latest["status"] == "paused_proof":
                time.sleep(0.1)
                continue
            if not (
                valid_handoff(seed_record, authorization)
                and authorization["gpu_phase_id"] == guard.record["gpu_phase_id"]
                and authorization["gpu_deadline_epoch"]
                == guard.record["gpu_deadline_epoch"]
                and latest["status"] == "data_ownership_transferred"
                and latest["gpu_phase_id"] == guard.record["gpu_phase_id"]
                and exited["phase_id"] == seed_record["cpu_phase_id"]
                and exited.get("transferred") is True
            ):
                raise RuntimeError(
                    "Parent authorization or CPU watchdog exit does not bind this GPU phase"
                )
            # Read only small metadata from the proven immutable volume. Never
            # redownload the HF checkpoint or rehash large shards on a GPU.
            import chalkcompute as cc
            from chalkcompute._weights_format import read_pointer, version_dir

            volume = cc.Volume(seed_record["name"], create_if_missing=False)
            volume.reload()
            if volume.version.id != proof["snapshot_id"]:
                raise RuntimeError("Seed volume changed after immutable CPU proof")
            snapshot = volume.at_version(proof["snapshot_id"])
            if read_pointer(snapshot.read_file("CURRENT")) != 0:
                raise RuntimeError("Transferred source CURRENT changed")
            manifest = snapshot.read_file(version_dir(0) + "/manifest.json")
            if hashlib.sha256(manifest).hexdigest() != proof["manifest_sha256"]:
                raise RuntimeError("Transferred manifest differs from CPU proof")
            guard.record["cpu_seed_authorization"] = authorization
            guard.record["cpu_seed_watchdog_exit"] = exited
            guard.record["status"] = "cpu_seed_transfer_verified"
            guard.save()
            return
        time.sleep(1)
    raise TimeoutError("CPU input lease expired before authorized GPU handoff")
