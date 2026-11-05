"""CPU reserve, live capacity and canonical cleanup identity regression tests."""

import copy
import json
import time
from types import SimpleNamespace as NS

import pytest

import profile_capacity as capacity
import profile_cpu_lifecycle as lifecycle
import profile_host_guard as host
from profile_plan import write_json


def sample_capacity():
    return {
        "memory": {"MemAvailable": 256 * 1024**3},
        "cpu_affinity_count": 32,
        "filesystems": {"/var/lib/docker": {"free_bytes": 200 * 1024**3}},
        "cgroup_v2": [
            {
                "memory.max": str(256 * 1024**3),
                "memory.current": str(16 * 1024**3),
                "cpu.max": "3200000 100000",
            },
            {"memory.max": "max", "cpu.max": "max 100000"},
        ],
    }


def test_actual_host_and_ancestor_cgroup_capacity_pass():
    assert capacity.validate_capacity(sample_capacity())
    compile(capacity.CAPACITY_CODE, "remote-capacity", "exec")


@pytest.mark.parametrize(
    "bad",
    [
        "physical_memory",
        "affinity",
        "disk",
        "ancestor_memory",
        "ancestor_cpu",
        "no_cgroup",
    ],
)
def test_actual_host_and_cgroup_shortage_fail_closed(bad):
    value = sample_capacity()
    if bad == "physical_memory":
        value["memory"]["MemAvailable"] = 127 * 1024**3
    elif bad == "affinity":
        value["cpu_affinity_count"] = 15
    elif bad == "disk":
        value["filesystems"]["/var/lib/docker"]["free_bytes"] = 119 * 1024**3
    elif bad == "ancestor_memory":
        value["cgroup_v2"][1] = {
            "memory.max": str(128 * 1024**3),
            "memory.current": "1",
        }
    elif bad == "ancestor_cpu":
        value["cgroup_v2"][1] = {"cpu.max": "1500000 100000"}
    else:
        value["cgroup_v2"] = []
    with pytest.raises(RuntimeError):
        capacity.validate_capacity(value)


@pytest.mark.parametrize("part", ["host", "pool"])
def test_placement_free_cpu_and_memory_required_at_both_scopes(part):
    free = {"cpu_millicores": 16000, "memory_bytes": 128 * 1024**3}
    pool = {"resources": {"free": copy.deepcopy(free)}}
    actual = {"resources": {"free": copy.deepcopy(free)}}
    assert host.require_free_placement(pool, actual)
    target = pool if part == "pool" else actual
    target["resources"]["free"]["cpu_millicores"] = 15999
    with pytest.raises(RuntimeError, match="placement"):
        host.require_free_placement(pool, actual)


def test_profile_poll_refuses_to_start_inside120_second_reserve(tmp_path):
    record = {"cpu_deadline_epoch": time.time() + 119}
    seed = NS(exec=lambda *a, **k: pytest.fail("poll inside reserve"))
    with pytest.raises(TimeoutError, match="120-second"):
        lifecycle.poll(seed, record, tmp_path / "launcher.json", "poll")


def test_profile_poll_that_finishes_after_work_cutoff_cannot_claim_success(
    tmp_path, monkeypatch
):
    now = [100.0]
    monkeypatch.setattr(lifecycle.time, "time", lambda: now[0])
    record = {"cpu_deadline_epoch": 230, "cpu_phase_id": "phase"}
    done = {"phase_id": "phase", "finished_at": 115, "status": "passed"}

    def run(*a, **kwargs):
        assert kwargs["timeout_secs"] == 10
        now[0] = 111
        return NS(
            error=None,
            exit_code=0,
            stdout_text=json.dumps({"stdout": "", "stderr": "", "completed": done}),
        )

    with pytest.raises(TimeoutError):
        lifecycle.poll(NS(exec=run), record, tmp_path / "launcher.json", "poll")
    assert "guest_completion" not in record


def test_profile_create_rpc_deadline_leaves120_second_reserve_and_persists_lost_response(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(lifecycle.time, "time", lambda: 100.0)
    monkeypatch.setattr(lifecycle.os, "kill", lambda *a: None)
    record = {
        "cpu_deadline_epoch": 230,
        "cpu_phase_id": "phase",
        "cpu_watchdog_pid": 123,
    }
    write_json(tmp_path / "cpu-watchdog-ready.json", {"phase_id": "phase", "pid": 123})

    def create(*a, **kwargs):
        assert kwargs["timeout_ms"] == 10000
        assert json.loads((tmp_path / "launcher.json").read_bytes())[
            "submission_inflight"
        ]
        raise TimeoutError

    stub = lifecycle.ProfileSandboxStub(
        NS(create_once=create), record, tmp_path / "launcher.json"
    )
    with pytest.raises(TimeoutError):
        stub.create_sandbox(None)
    assert record["create_rpc_deadline_epoch"] == 110
    assert record["create_reconcile_until_epoch"] == 170
    assert record["submission_inflight"]
    with pytest.raises(RuntimeError, match="replay refused"):
        stub.create_sandbox(None)


def test_recovered_uuid_uid_survive_cleanup_subdir_transient_and_canonical_reload(
    tmp_path, monkeypatch
):
    canonical = tmp_path / "launcher.json"
    child = tmp_path / "cpu-watchdog-cleanup"
    child.mkdir()
    record = {
        "name": "weight-profile-test",
        "cpu_phase_id": "phase",
        "environment_id": "qa",
        "cpu_deadline_epoch": 2000,
        "kube_context": "ctx",
        "fallback_kube_context": "ctx",
        "submission_inflight": True,
    }
    write_json(canonical, record)
    resource = {"context": "ctx", "name": "cc-owned-id", "uid": "immutable-uid"}

    class Capture:
        def __init__(self, name, out, record, *args):
            self.record = record

        def capture_container_resource(self, *args):
            self.record["seed_resources"] = [resource]

    calls = [0]

    def release(record, out):
        calls[0] += 1
        assert json.loads(canonical.read_bytes())["seed_resources"] == [resource]
        if calls[0] == 1:
            raise RuntimeError("API association vanished after transient")
        record["cpu_compute_released"] = True

    def recover(record):
        record["seed_sandbox_id"] = "owned-id"

    guard = NS(
        release_cpu=release,
        recover_seed=recover,
        ResourceGuard=Capture,
        write_json=write_json,
    )
    monkeypatch.setattr(host, "runtime_absence", lambda id: {"id": id, "absent": True})
    host.install_release_check(guard, canonical)
    with pytest.raises(RuntimeError):
        guard.release_cpu(record, child)
    retained = json.loads(canonical.read_bytes())
    assert retained["seed_sandbox_id"] == "owned-id"
    assert retained["seed_resources"] == [resource]
    guard.release_cpu(retained, child)
    assert json.loads(canonical.read_bytes())["host_runtime_verified_absent"]


def test_canonical_merge_refuses_reused_uuid_or_owner_uid():
    base = {
        "name": "n",
        "cpu_phase_id": "p",
        "environment_id": "e",
        "cpu_deadline_epoch": 2000,
        "seed_sandbox_id": "id",
        "seed_resources": [{"context": "c", "name": "cc-id", "uid": "old"}],
    }
    for changed in (
        dict(base, seed_sandbox_id="other"),
        dict(base, seed_resources=[{"context": "c", "name": "cc-id", "uid": "new"}]),
    ):
        with pytest.raises(RuntimeError):
            host.merge_retained_identity(changed, base)


def recovery_record():
    import hashlib

    phase = "f" * 32
    program = "print(" + repr(phase) + ")"
    return {
        "seed_name": "weight-profile-" + phase[:10] + "-cpu",
        "cpu_phase_id": phase,
        "environment_id": "qa",
        "expected_namespaces": ["qa"],
        "kube_contexts": [host.CONTEXT],
        "guest_entrypoint_sha256": hashlib.sha256(program.encode()).hexdigest(),
        "submission_inflight": True,
        "training_deploy_image_uri": "unit-image@sha256:immutable",
        "create_reconcile_until_epoch": 190,
    }, program


def recovery_cr(record, program):
    id = "11111111-1111-1111-1111-111111111111"
    return {
        "metadata": {
            "name": "cc-" + id,
            "namespace": "qa",
            "uid": "immutable",
            "labels": {
                "chalk.ai/container-name": record["seed_name"],
                "chalk.ai/environment": "qa",
                "chalk.ai/container": id,
            },
        },
        "spec": {
            "entrypoint": ["python3", "-u", "-c", program],
            "image": record["training_deploy_image_uri"],
        },
    }


def test_exact_label_recovery_binds_phase_frozen_program_environment_and_owner(
    tmp_path, monkeypatch
):
    import subprocess

    record, program = recovery_record()
    cr = recovery_cr(record, program)

    def run(argv, **kwargs):
        assert "chalk.ai/container-name=" + record["seed_name"] in argv
        assert kwargs["timeout"] <= 6
        return NS(stdout=json.dumps({"items": [cr]}))

    monkeypatch.setattr(subprocess, "run", run)
    assert (
        lifecycle.recover_seed(record) == cr["metadata"]["labels"]["chalk.ai/container"]
    )
    assert record["seed_resources"][0]["uid"] == "immutable"


@pytest.mark.parametrize(
    "change", ["namespace", "environment", "name", "program", "uid_drift", "multiple"]
)
def test_exact_label_recovery_refuses_wrong_ownership_or_ambiguity(monkeypatch, change):
    import subprocess

    record, program = recovery_record()
    cr = recovery_cr(record, program)
    if change == "namespace":
        cr["metadata"]["namespace"] = "other"
    elif change == "environment":
        cr["metadata"]["labels"]["chalk.ai/environment"] = "other"
    elif change == "name":
        cr["metadata"]["labels"]["chalk.ai/container-name"] = "other"
    elif change == "program":
        cr["spec"]["entrypoint"][3] += "\n#changed"
    elif change == "uid_drift":
        record["seed_resources"] = [
            {"context": host.CONTEXT, "name": cr["metadata"]["name"], "uid": "reused"}
        ]
    items = [cr, cr] if change == "multiple" else [cr]
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: NS(stdout=json.dumps({"items": items}))
    )
    with pytest.raises(RuntimeError):
        lifecycle.recover_seed(record)
    assert record.get("seed_sandbox_id") is None


def test_zero_match_recovery_preserves_ambiguity_and_original_window(monkeypatch):
    import subprocess

    record, program = recovery_record()
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: NS(stdout='{"items":[]}'))
    assert lifecycle.recover_seed(record) is None
    assert record["submission_inflight"] is True
    assert record["create_reconcile_until_epoch"] == 190


def test_cpu_release_retains_uuid_uid_canonically_before_exact_termination(
    tmp_path, monkeypatch
):
    canonical = tmp_path / "launcher.json"
    child = tmp_path / "child"
    child.mkdir()
    record = {
        "name": "weight-profile-test",
        "seed_name": "weight-profile-test-cpu",
        "cpu_phase_id": "phase",
        "environment_id": "qa",
        "cpu_deadline_epoch": 2000,
        "kube_context": "ctx",
        "fallback_kube_context": "ctx",
        "seed_sandbox_id": "owned",
    }
    write_json(canonical, record)

    class Guard:
        def __init__(self, name, out, record, *args):
            self.record = record

        def capture_container_resource(self, *args):
            self.record["seed_resources"] = [
                {"context": "ctx", "name": "cc-owned", "uid": "immutable"}
            ]

        def verify_container_absent(self, *args):
            self.record["seed_resources_verified_absent"] = True

    def rpc(id, **kwargs):
        saved = json.loads(canonical.read_bytes())
        assert saved["seed_sandbox_id"] == "owned"
        assert saved["seed_resources"][0]["uid"] == "immutable"
        return {"id": id, "status": "terminated"}

    monkeypatch.setattr(lifecycle, "sandbox_rpc", rpc)
    lifecycle.release_cpu(NS(ResourceGuard=Guard), record, child, canonical)
    assert record["cpu_compute_released"]
