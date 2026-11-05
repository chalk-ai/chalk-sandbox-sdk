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
            assert 0 < kwargs["timeout_ms"] <= 60000
            assert (
                persisted["create_reconcile_until_epoch"]
                == persisted["create_rpc_deadline_epoch"] + 60
            )
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


def test_late_success_persists_id_but_refuses_work_after_deadline(tmp_path):
    record, _ = handoff()
    ledger = tmp_path / "launcher.json"
    write_json(
        tmp_path / "cpu-watchdog-ready.json",
        {"phase_id": record["cpu_phase_id"], "pid": os.getpid()},
    )

    class Stub:
        def create_sandbox(self, request, **kwargs):
            record["cpu_deadline_epoch"] = time.time() - 1
            return SimpleNamespace(sandbox=SimpleNamespace(id="late-exact-id"))

    with pytest.raises(TimeoutError):
        GuardedSandboxStub(Stub(), record, ledger).create_sandbox("request")
    persisted = json.loads(ledger.read_text())
    assert persisted["seed_sandbox_id"] == "late-exact-id"
    assert persisted["submission_inflight"] is False


def test_filtered_hf_cache_proves_seed_without_excluded_readme_or_license(
    tmp_path, monkeypatch, capsys
):
    import fnmatch
    import huggingface_hub
    import safetensors
    import xxhash
    import chalkcompute.weights as weights
    from chalkcompute._weights import _TensorSpec
    from chalkcompute._weights_format import (
        WeightManifest,
        TensorEntry,
        architecture_hash,
        pointer_json,
    )
    from cpu_seed import SEED_CODE

    config = b'{"model_type":"test"}'
    payload = b"\x00\x00"
    (tmp_path / "config.json").write_bytes(config)
    (tmp_path / "model.safetensors").write_bytes(payload)
    upstream = {
        "config.json",
        "model.safetensors",
        "README.md",
        "LICENSE",
        ".gitattributes",
    }
    cached = {"config.json", "model.safetensors"}
    calls = []

    def offline_cache(repo_id, revision, local_files_only, allow_patterns=None):
        assert (
            repo_id == "test/model" and revision == "pinned-commit" and local_files_only
        )
        needed = {
            name
            for name in upstream
            if allow_patterns is None
            or any(fnmatch.fnmatch(name, pattern) for pattern in allow_patterns)
        }
        missing = needed - cached
        if missing:
            raise RuntimeError("Incomplete snapshot: " + ",".join(sorted(missing)))
        calls.append("filtered-cache-reopened")
        return str(tmp_path)

    digest = xxhash.xxh3_128_hexdigest(payload)
    manifest = WeightManifest(
        0,
        None,
        "keyframe",
        "test/model",
        architecture_hash(config, [("weight", _TensorSpec((1,), "bfloat16"))]),
        (
            TensorEntry(
                "weight",
                "bfloat16",
                (1,),
                "dense",
                "model.safetensors",
                0,
                len(payload),
                1,
                digest,
            ),
        ),
    )
    files = {
        "CURRENT": pointer_json(0),
        "v000000/manifest.json": manifest.to_json(),
        "v000000/config.json": config,
        "v000000/model.safetensors": payload,
    }

    class Snapshot:
        version_id = 42

        def read_file(self, path):
            return files[path]

        def listdir(self):
            return [SimpleNamespace(path=path, kind="file") for path in files]

    class Stream:
        def __init__(self, name, model):
            pass

        def create(self):
            return self

        def seed_from_hf(self, model, revision):
            return 0

        def _snapshot(self):
            return Snapshot()

    class SafeOpen:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def keys(self):
            return ["weight"]

        def get_slice(self, key):
            return SimpleNamespace(get_dtype=lambda: "BF16", get_shape=lambda: [1])

    monkeypatch.setattr(huggingface_hub, "snapshot_download", offline_cache)
    monkeypatch.setattr(safetensors, "safe_open", SafeOpen)
    monkeypatch.setattr(weights, "WeightStream", Stream)
    exec(
        compile(SEED_CODE, "guest-cpu-proof", "exec"),
        {"CONFIG": ("owned", "test/model", "pinned-commit")},
    )
    assert calls == ["filtered-cache-reopened"]
    output = capsys.readouterr().out
    proof = json.loads(
        next(
            line.removeprefix("CPU_SEED_PROOF=")
            for line in output.splitlines()
            if line.startswith("CPU_SEED_PROOF=")
        )
    )
    assert proof["snapshot_id"] == 42 and proof["elements"] == 1
    assert proof["files"]["model.safetensors"]["bytes"] == 2
