"""Lost read responses and long seeds cannot repeat entrypoint publication."""

import json

import pytest

import cpu_seed_guest


def test_lost_exec_poll_response_and_over_300s_seed_never_replay_publication(
    tmp_path, monkeypatch
):
    clock = [1000.0]
    invocations = []
    monkeypatch.setattr(cpu_seed_guest.time, "time", lambda: clock[0])

    def advance():
        invocations.append("seed")
        clock[0] += 301

    code = (
        "CONFIG(); proof={'current':0,'snapshot_id':42}; print('finished',flush=True)"
    )
    assert cpu_seed_guest.run_seed_once(tmp_path, "phase", code, advance)
    first = cpu_seed_guest.read_progress(tmp_path)
    assert first["completed"]["elapsed_seconds"] == 301
    assert first["completed"]["status"] == "passed"

    def lost_response():
        # Simulate the server completing a read-only ExecCommand, followed by
        # UNAVAILABLE before its response reaches the client.
        cpu_seed_guest.read_progress(tmp_path)
        raise ConnectionError("response lost")

    with pytest.raises(ConnectionError):
        lost_response()
    replay = cpu_seed_guest.read_progress(tmp_path)
    assert replay["proof"] == {"current": 0, "snapshot_id": 42}
    assert invocations == ["seed"]
    # An accidental repeated entrypoint cannot publish again either.
    assert not cpu_seed_guest.run_seed_once(tmp_path, "phase", code, advance)
    assert invocations == ["seed"]


def test_failed_proof_is_durable_and_a_read_retry_does_not_restart(tmp_path):
    calls = []

    def fail():
        calls.append("once")
        raise RuntimeError("proof failed")

    assert cpu_seed_guest.run_seed_once(tmp_path, "phase", "CONFIG()", fail)
    first = cpu_seed_guest.read_progress(tmp_path)
    second = cpu_seed_guest.read_progress(tmp_path)
    assert first["completed"] == second["completed"]
    assert first["completed"]["status"] == "failed"
    assert "RuntimeError: proof failed" in first["stderr"]
    assert "proof" not in first
    assert calls == ["once"]


def test_durable_proof_tamper_is_rejected(tmp_path):
    cpu_seed_guest.run_seed_once(tmp_path, "phase", "proof={'snapshot_id':42}", None)
    (tmp_path / "proof.json").write_text(json.dumps({"snapshot_id": 43}))
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        cpu_seed_guest.read_progress(tmp_path)


def test_exclusive_marker_without_ready_metadata_is_pending_and_never_reruns(tmp_path):
    (tmp_path / "started.lock").write_text("")
    pending = cpu_seed_guest.read_progress(tmp_path)
    assert "started" not in pending and "completed" not in pending
    assert not cpu_seed_guest.run_seed_once(
        tmp_path, "phase", "raise AssertionError('must not run')", None
    )
    assert not (tmp_path / "completed.json").exists()


def test_partial_atomic_progress_line_is_pending(tmp_path):
    (tmp_path / "stdout.txt").write_text("CPU_SEED_ATOMIC={")
    assert "atomic_seed" not in cpu_seed_guest.read_progress(tmp_path)
