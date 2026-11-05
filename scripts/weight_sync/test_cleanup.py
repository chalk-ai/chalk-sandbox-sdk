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
