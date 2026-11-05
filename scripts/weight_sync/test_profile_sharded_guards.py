"""Small-plan cleanup retries, absolute deadline, and measured resource gates."""

import json
from types import SimpleNamespace as NS

import pytest

import profile_sharded_guard as guard
from profile_sharded_plan import runtime_capacity_code, validate_runtime_limits


def record(tmp_path, monkeypatch, *, deadline=1100):
    value = {
        "cpu_phase_id": "a" * 32,
        "cpu_deadline_epoch": deadline,
        "launcher_pid": 101,
        "launcher_identity": {"pid": 101, "group": 101, "started": "known"},
        "environment_id": "test",
        "name": "weight-profile-test",
        "kube_context": "test-context",
        "fallback_kube_context": "test-context",
        "submission_inflight": True,
    }
    ledger = tmp_path / "launcher.json"
    guard.write_json(ledger, value)
    import profile_sharded_plan as plan

    monkeypatch.setattr(plan, "validate_ledger", lambda p: json.loads(p.read_bytes()))
    clock = [1000.0]
    monkeypatch.setattr(
        guard,
        "time",
        NS(time=lambda: clock[0], sleep=lambda v: clock.__setitem__(0, clock[0] + v)),
    )
    return ledger, clock


def test_cleanup_retries_timeout_late_id_native_failure_and_volume_failure(
    tmp_path, monkeypatch
):
    ledger, _ = record(tmp_path, monkeypatch)
    observed = []

    def attempt(path, value, number):
        observed.append((number, value.get("seed_sandbox_id")))
        if number == 0:
            raise TimeoutError("first API timeout")
        if number == 1:
            value.update(
                seed_sandbox_id="late-owned-id",
                seed_resources=[{"uid": "retained-owner"}],
            )
            guard.write_json(path, value)
            raise RuntimeError("transient native absence failure")
        if number == 2:
            assert value["seed_resources"] == [{"uid": "retained-owner"}]
            raise RuntimeError("transient volume delete failure")
        value.update(
            status="released", cpu_compute_released=True, volume_verified_absent=True
        )
        guard.write_json(path, value)

    monkeypatch.setattr(guard, "run_attempt", attempt)
    result = guard.cleanup(ledger)
    assert result["released"] is True and len(result["errors"]) == 3
    assert observed == [
        (0, None),
        (1, None),
        (2, "late-owned-id"),
        (3, "late-owned-id"),
    ]
    assert all(v["phase_id"] == "a" * 32 for v in result["errors"])
    assert json.loads(ledger.read_bytes())["seed_resources"] == [
        {"uid": "retained-owner"}
    ]


def test_unknown_uuid_deadline_never_claims_release_and_retains_history(
    tmp_path, monkeypatch
):
    ledger, clock = record(tmp_path, monkeypatch, deadline=1006)
    attempts = []

    def attempt(path, value, number):
        attempts.append(number)
        clock[0] += 1
        raise RuntimeError("unknown UUID remains unproved")

    monkeypatch.setattr(guard, "run_attempt", attempt)
    with pytest.raises(RuntimeError, match="Absolute600-second cleanup deadline"):
        guard.cleanup(ledger)
    value = json.loads(ledger.read_bytes())
    assert value["status"] == "cleanup_unconfirmed"
    assert value["cpu_deadline_epoch"] == 1006
    assert "cpu_compute_released" not in value and "volume_verified_absent" not in value
    history = json.loads((tmp_path / "cleanup-history.json").read_bytes())
    assert history["released"] is False and len(history["errors"]) == len(attempts) >= 2


def test_launcher_death_with_inflight_create_still_enters_guarded_cleanup(
    tmp_path, monkeypatch
):
    import chalkcompute._connect as connect
    import profile_host_guard as host

    ledger, _ = record(tmp_path, monkeypatch)
    monkeypatch.setattr(
        guard, "install", lambda p: NS(ResourceGuard=lambda *args: NS(kube_contexts=[]))
    )
    monkeypatch.setattr(
        connect, "ConnectClient", lambda: NS(_ensure_auth=lambda: None, _env_id="test")
    )
    monkeypatch.setattr(host, "runtime_absence", lambda _: {})
    monkeypatch.setattr(guard, "process_identity", lambda pid: None)
    calls = []

    def stop(value):
        assert (tmp_path / "cpu-expired").exists()
        assert value["submission_inflight"] is True
        calls.append("stop-original-writer")

    def cleanup(path):
        calls.append("reconcile-compute-and-data")
        value = json.loads(path.read_bytes())
        value.update(
            status="released", cpu_compute_released=True, volume_verified_absent=True
        )
        guard.write_json(path, value)

    monkeypatch.setattr(guard, "stop_launcher", stop)
    monkeypatch.setattr(guard, "cleanup", cleanup)
    guard.run_watchdog(ledger)
    assert calls == ["stop-original-writer", "reconcile-compute-and-data"]
    assert (tmp_path / "cpu-watchdog-exited.json").exists()


def test_stop_launcher_refuses_reused_pid(tmp_path, monkeypatch):
    ledger, _ = record(tmp_path, monkeypatch)
    monkeypatch.setattr(
        guard, "process_identity", lambda pid: {"pid": pid, "started": "different"}
    )
    monkeypatch.setattr(
        guard.os, "kill", lambda *args: pytest.fail("unrelated process signal")
    )
    with pytest.raises(RuntimeError, match="identity changed"):
        guard.stop_launcher(json.loads(ledger.read_bytes()))


def test_real_runtime_limits_require_bounded_memory_cpu_and_peak_counters():
    value = {
        "cpu_affinity_count": 255,
        "cgroup_v2": [
            {
                "memory.max": str(8 * 1024**3),
                "memory.current": "100",
                "memory.peak": "200",
                "cpu.max": "400000 100000",
            }
        ],
    }
    assert validate_runtime_limits(value) is value
    del value["cgroup_v2"][0]["memory.peak"]
    with pytest.raises(RuntimeError, match="peak counters"):
        validate_runtime_limits(value)
    value["cgroup_v2"][0]["memory.peak"] = "200"
    value["cgroup_v2"][0]["memory.max"] = "max"
    with pytest.raises(RuntimeError, match="memory limit"):
        validate_runtime_limits(value)
    assert "'memory.peak'" in runtime_capacity_code()
