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
