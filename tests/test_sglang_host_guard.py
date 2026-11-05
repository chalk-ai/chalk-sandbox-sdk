from __future__ import annotations

import importlib.util
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "sglang_host_guard", Path(__file__).parents[1] / "scripts/sglang/host_guard.py"
)
assert spec and spec.loader
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


@pytest.fixture
def pool(monkeypatch):
    timestamp = datetime.now(timezone.utc).isoformat()
    value = {
        "spec": {"external": {}},
        "status": {
            "capacity": {
                "observed_at": timestamp,
                "hosts": [
                    {
                        "host_id": host.HOST,
                        "accepting_placements": True,
                        "external_control_stream_connected": True,
                        "last_heartbeat_at": timestamp,
                        "resources": {"total": {"gpus": {"nvidia-a100-80gb": 8}}},
                    }
                ],
            }
        },
    }
    monkeypatch.setattr(
        host.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout=json.dumps(value)),
    )
    return value


def test_only_fresh_single_oracle_membership_accepted(pool):
    assert host.membership()["singleton_pool"] is True


@pytest.mark.parametrize(
    "change",
    [
        "second_host",
        "amd",
        "stale_status",
        "stale_heartbeat",
        "not_accepting",
        "disconnected",
        "wrong_host",
        "environment_pool",
    ],
)
def test_changed_or_stale_placement_inventory_refuses_claim(pool, change):
    capacity = pool["status"]["capacity"]
    item = capacity["hosts"][0]
    if change == "second_host":
        capacity["hosts"].append(dict(item))
    elif change == "amd":
        item["resources"]["total"]["gpus"] = {"amd-mi355x": 8}
    elif change == "stale_status":
        capacity["observed_at"] = "2000-01-01T00:00:00Z"
    elif change == "stale_heartbeat":
        item["last_heartbeat_at"] = "2000-01-01T00:00:00Z"
    elif change == "not_accepting":
        item["accepting_placements"] = False
    elif change == "disconnected":
        item["external_control_stream_connected"] = False
    elif change == "wrong_host":
        item["host_id"] = "other-host"
    elif change == "environment_pool":
        pool["spec"]["external"] = {"environment_id": "other-env"}
    with pytest.raises(ValueError, match="membership/freshness"):
        host.membership()


@pytest.mark.parametrize("remaining", ["runsc", "task", None])
def test_native_absence_requires_both_runtime_and_task_inventory(
    monkeypatch, remaining
):
    identity = "01234567-0123-0123-0123-0123456789ab"
    outputs = [
        json.dumps([{"id": "cc-" + identity}] if remaining == "runsc" else []),
        "123\n",
        "k8s.io\n",
        identity + "\n" if remaining == "task" else "",
    ]
    monkeypatch.setattr(host, "ssh", lambda args: outputs.pop(0))
    if remaining:
        with pytest.raises(RuntimeError, match="remains"):
            host.runtime_absence(identity)
    else:
        proof = host.runtime_absence(identity)
        assert proof["runsc_absent"] and proof["containerd_task_absent"]
        assert not outputs


def test_unknown_native_inventory_is_not_absence(monkeypatch):
    def failed(args):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(host, "ssh", failed)
    with pytest.raises(subprocess.CalledProcessError):
        host.runtime_absence("01234567-0123-0123-0123-0123456789ab")


def test_actual_native_identity_matches_exact_id_not_substring(monkeypatch):
    identity = "01234567-0123-0123-0123-0123456789ab"
    monkeypatch.setattr(
        host, "ssh", lambda args: json.dumps([{"id": "other-" + identity, "pid": 123}])
    )
    with pytest.raises(RuntimeError, match="identity not present"):
        host.runtime_identity(identity)
    monkeypatch.setattr(
        host,
        "ssh",
        lambda args: json.dumps(
            [
                {
                    "id": "cc-" + identity,
                    "pid": 123,
                    "status": "running",
                    "unrelated": "not exported",
                }
            ]
        ),
    )
    assert host.runtime_identity(identity) == {
        "id": "cc-" + identity,
        "pid": 123,
        "status": "running",
    }


def test_host_release_failure_cannot_disarm_compute_guard(tmp_path, monkeypatch):
    record = {"seed_sandbox_id": "01234567-0123-0123-0123-0123456789ab"}

    def original(value, out):
        value["cpu_compute_released"] = True

    def write(path, value):
        path.write_text(json.dumps(value))

    guard = SimpleNamespace(release_cpu=original, write_json=write)
    host.install_release_check(guard)

    def still_running(identity):
        raise RuntimeError("owned runtime remains")

    monkeypatch.setattr(host, "runtime_absence", still_running)
    with pytest.raises(RuntimeError, match="remains"):
        guard.release_cpu(record, tmp_path)
    assert record["cpu_compute_released"] is False
    assert not record.get("host_runtime_verified_absent")
    assert not (tmp_path / "cpu-watchdog-disarmed.json").exists()


@pytest.mark.parametrize(
    "placement",
    [
        None,
        {},
        {"host_id": "other"},
        {"host_id": host.HOST, "pool_id": host.POOL, "state": "Pending"},
    ],
)
def test_actual_placement_requires_committed_oracle_identity(placement):
    with pytest.raises(RuntimeError, match="HOST placement differs"):
        host.require_placement([placement])
    good = {"host_id": host.HOST, "pool_id": host.POOL, "state": "Committed"}
    assert host.require_placement([good]) == good


def test_detached_watchdog_uses_host_release_wrapper(tmp_path, monkeypatch):
    identity = "01234567-0123-0123-0123-0123456789ab"
    record = {"seed_sandbox_id": identity}
    probes = []

    def native(container_id):
        probes.append(container_id)
        return {"runsc_absent": True, "containerd_task_absent": True}

    monkeypatch.setattr(host, "runtime_absence", native)

    def write(path, value):
        path.write_text(json.dumps(value))

    guard = SimpleNamespace(release_cpu=lambda record, out: None, write_json=write)

    def watchdog(ledger):
        guard.release_cpu(record, ledger.parent)

    guard.watchdog = watchdog
    host.run_watchdog(guard, tmp_path / "launcher.json")
    assert probes == [None, identity]
    assert record["host_runtime_verified_absent"] is True
    assert record["cpu_compute_released"] is True
