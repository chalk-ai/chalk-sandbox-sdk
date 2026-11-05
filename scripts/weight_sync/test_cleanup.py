"""CPU-only resource-cleanup regressions; no SDK or infrastructure calls."""

import subprocess
import sys
import time

from training_smoke import release_compute_then_retain


def test_blocked_evidence_reader_cannot_delay_compute_release(monkeypatch, tmp_path):
    events = []

    class Guard:
        name = "owned-test"
        out = tmp_path
        record = {}

        def cleanup_compute(self):
            events.append("compute_released")

        def save(self):
            pass

    original_run = subprocess.run

    def blocked_reader(*args, **kwargs):
        assert events == ["compute_released"]
        events.append("evidence_reader_started")
        # Exercise an actual blocked child and subprocess timeout/reaping.
        return original_run(
            [sys.executable, "-c", "import time; time.sleep(60)"], **kwargs
        )

    monkeypatch.setattr(subprocess, "run", blocked_reader)
    started = time.monotonic()
    guard = Guard()
    release_compute_then_retain(guard, timeout=0.05)
    assert time.monotonic() - started < 1
    assert guard.record["evidence_retention"] == "timed_out"
    assert events == ["compute_released", "evidence_reader_started"]


def test_cleanup_owns_leased_input_only_after_explicit_transfer(monkeypatch, tmp_path):
    import chalkcompute as cc
    import chalkcompute._connect as connect
    from chalkcompute._volume import VolumeNotFoundError
    from training_smoke import cleanup_data

    touched = []
    deleted = set()

    class Volume:
        def __init__(self, name, **kwargs):
            self.name = name
            touched.append(name)

        def reload(self):
            if self.name in deleted:
                raise VolumeNotFoundError("deleted")

        def delete(self):
            deleted.add(self.name)

    class Guard:
        name = "leased-source"
        dataset_id = None
        record = {"owns_source_volume": False}

    monkeypatch.setattr(cc, "Volume", Volume)
    monkeypatch.setattr(connect, "ConnectClient", lambda: None)
    guard = Guard()
    assert cleanup_data(guard) == []
    assert guard.name not in touched
    assert len(deleted) == 3
    guard.record["owns_source_volume"] = True
    assert cleanup_data(guard) == []
    assert guard.name in deleted


def test_dead_detached_watchdog_refuses_claims_even_with_stale_ready_file(tmp_path):
    import json
    from training_smoke import ResourceGuard

    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=5)
    record = {"name": "owned", "gpu_phase_id": "gpu-phase", "watchdog_pid": process.pid}
    (tmp_path / "watchdog-ready").write_text(
        json.dumps({"phase_id": "gpu-phase", "pid": process.pid})
    )
    guard = ResourceGuard("owned", tmp_path, record, "test", "fallback")
    guard.watchdog_process = process
    import pytest

    with pytest.raises(RuntimeError, match="watchdog has exited"):
        guard.check_budget()


def test_ambiguous_dataset_finalize_recovers_only_exact_owned_name(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace
    import chalkcompute as cc
    import chalkcompute._connect as connect
    from chalkcompute._gen.chalk.server.v1 import datasets_pb as dp
    from chalkcompute._volume import VolumeNotFoundError
    from training_smoke import cleanup_data

    deleted = []

    class Client:
        def stub(self, kind):
            return SimpleNamespace(list_datasets="list", delete_dataset="delete")

        def call(self, method, request):
            if method == "delete":
                deleted.append(request.dataset_id)
                return None
            if request.filters.ids:
                assert request.filters.ids == ["owned-id"]
                return dp.ListDatasetsResponse()
            assert request.filters.search == "leased-source"
            return dp.ListDatasetsResponse(
                datasets=[
                    dp.DatasetMeta(id="other-id", dataset_name="leased-source-other"),
                    dp.DatasetMeta(id="owned-id", dataset_name="leased-source"),
                ]
            )

    def absent_volume(name, **kwargs):
        raise VolumeNotFoundError("absent")

    guard = SimpleNamespace(
        name="leased-source",
        out=tmp_path,
        dataset_id=None,
        record={
            "owns_source_volume": False,
            "submission_inflight": {"finalize_dataset_upload": True},
        },
    )
    monkeypatch.setattr(connect, "ConnectClient", Client)
    monkeypatch.setattr(cc, "Volume", absent_volume)
    assert cleanup_data(guard) == []
    assert deleted == ["owned-id"]
    assert guard.record["dataset_deleted"] is True


def test_data_error_keeps_guard_armed_until_a_successful_retry(monkeypatch, tmp_path):
    import pytest
    import training_smoke

    class Guard:
        out = tmp_path
        record = {"cpu_seed_ledger": "exact-ledger", "owns_source_volume": True}

        def save(self):
            pass

    guard = Guard()
    attempts = []

    def cleanup(_guard):
        attempts.append("attempt")
        return ["volume deletion unconfirmed"] if len(attempts) == 1 else []

    monkeypatch.setattr(training_smoke, "cleanup_data", cleanup)
    with pytest.raises(RuntimeError, match="watchdog remains armed"):
        training_smoke.cleanup_data_and_disarm(guard)
    assert not (tmp_path / "watchdog-disarmed").exists()
    assert guard.record["cleanup_errors"]
    training_smoke.cleanup_data_and_disarm(guard)
    assert (tmp_path / "watchdog-disarmed").exists()
    assert guard.record["cleanup_errors"] == []
