"""Read-only CPU-only release proofs, ownership windows and fresh native checks."""

import time
from types import SimpleNamespace as NS

import pytest

import profile_verify as verify
from profile_plan import write_json


def cpu_record():
    return {
        "name": "weight-profile-test",
        "seed_name": "weight-profile-test-cpu",
        "cpu_phase_id": "phase",
        "environment_id": "qa",
        "kube_context": "ctx",
        "fallback_kube_context": "ctx",
        "kube_contexts": ["ctx"],
        "seed_resources": [],
    }


def marker(name, status="inflight"):
    return {
        "phase_id": "phase",
        "name": name,
        "status": status,
        "started_at": 100,
        "rpc_deadline_epoch": 130,
        "reconcile_until_epoch": 190,
    }


@pytest.mark.parametrize("status", ["inflight", "acknowledged", "collision"])
def test_cpu_volume_ownership_respects_markers_and_collision(tmp_path, status):
    cpu = cpu_record()
    name = cpu["name"]
    write_json(verify.marker_path(tmp_path, name), marker(name, status))
    if status == "inflight":
        with pytest.raises(RuntimeError, match="late-create"):
            verify.volume_ownership(cpu, tmp_path, 189)
    owned, collisions = verify.volume_ownership(cpu, tmp_path, 190)
    assert owned == ([] if status == "collision" else [name])
    assert collisions == ([name] if status == "collision" else [])


def test_cpu_volume_ownership_rejects_changed_phase_or_reconciliation(tmp_path):
    cpu = cpu_record()
    value = marker(cpu["name"])
    value["phase_id"] = "other"
    write_json(verify.marker_path(tmp_path, cpu["name"]), value)
    with pytest.raises(RuntimeError, match="marker differs"):
        verify.volume_ownership(cpu, tmp_path, 200)


@pytest.fixture
def read_only_environment(monkeypatch):
    import chalkcompute as cc
    from chalkcompute._connect import ConnectClient
    from chalkcompute._volume import VolumeNotFoundError

    monkeypatch.setattr(
        ConnectClient, "_ensure_auth", lambda self: setattr(self, "_env_id", "qa")
    )
    monkeypatch.setattr(
        cc.Sandbox, "list_all", lambda: pytest.fail("unbounded enumeration")
    )
    monkeypatch.setattr(verify.ProfileGuard, "kube_items", lambda *a, **k: [])
    monkeypatch.setattr(verify, "process_identity", lambda pid: None)

    class Missing:
        def reload(self):
            raise VolumeNotFoundError("absent")

    monkeypatch.setattr(cc, "Volume", lambda *a, **k: Missing())
    return cc


def test_cpu_only_never_submitted_needs_no_gpu_ledger_or_sandbox_id(
    tmp_path, read_only_environment, monkeypatch
):
    ledger = tmp_path / "launcher.json"
    write_json(ledger, cpu_record())
    monkeypatch.setattr(
        verify.host,
        "runtime_absence",
        lambda *a: pytest.fail("no CPU submission occurred"),
    )
    result = verify.verify(ledger, None, tmp_path / "proof")
    assert result["mode"] == "cpu_only"
    assert result["cpu_native_absence"]["never_submitted"]
    assert result["all_owned_resources_absent"]


def test_cpu_only_unacknowledged_submission_fails_closed_without_bounded_name_api(
    tmp_path, read_only_environment
):
    ledger = tmp_path / "launcher.json"
    record = cpu_record()
    record.update(create_rpc_deadline_epoch=130, create_reconcile_until_epoch=190)
    write_json(ledger, record)
    with pytest.raises(RuntimeError, match="bounded exact-name"):
        verify.verify(ledger, None, tmp_path / "proof")


def test_cpu_only_ambiguous_create_before_reconciliation_cannot_certify(
    tmp_path, read_only_environment
):
    ledger = tmp_path / "launcher.json"
    record = cpu_record()
    record["create_reconcile_until_epoch"] = time.time() + 60
    write_json(ledger, record)
    with pytest.raises(RuntimeError, match="late-create"):
        verify.verify(ledger, None, tmp_path / "proof")


def test_cpu_only_known_id_uses_exact_get_and_fresh_native_absence(
    tmp_path, read_only_environment, monkeypatch
):
    ledger = tmp_path / "launcher.json"
    record = dict(cpu_record(), seed_sandbox_id="owned-id")
    write_json(ledger, record)
    gets, native = [], []
    monkeypatch.setattr(
        verify,
        "sandbox_status",
        lambda id: gets.append(id) or {"id": id, "status": "terminated"},
    )
    monkeypatch.setattr(
        verify.host,
        "runtime_absence",
        lambda id: native.append(id) or {"id": id, "absent": True},
    )
    result = verify.verify(ledger, None, tmp_path / "proof")
    assert gets == native == ["owned-id"]
    assert result["all_owned_resources_absent"]


def test_cpu_only_active_api_and_retained_owner_pod_block_proof(
    tmp_path, read_only_environment, monkeypatch
):
    ledger = tmp_path / "launcher.json"
    record = cpu_record()
    record["seed_resources"] = [{"context": "ctx", "name": "cc-id", "uid": "immutable"}]
    write_json(ledger, record)
    monkeypatch.setattr(
        verify.ProfileGuard,
        "kube_items",
        lambda self, kind, **kwargs: (
            [{"metadata": {"ownerReferences": [{"uid": "immutable"}]}}]
            if kind == "pods"
            else []
        ),
    )
    with pytest.raises(RuntimeError, match="pod remains"):
        verify.verify(ledger, None, tmp_path / "proof")


def test_full_proof_requeries_every_group_despite_stored_native_release_boolean(
    tmp_path, read_only_environment, monkeypatch
):
    ledger = tmp_path / "launcher.json"
    cpu = cpu_record()
    write_json(ledger, cpu)
    gpu = dict(
        cpu,
        cpu_seed_phase_id="phase",
        gpu_phase_id="gpu",
        group_ids={"live": "uuid"},
        profile_native_release_proofs={"uuid": {"old": True}},
    )
    gpu_path = tmp_path / "gpu.json"
    write_json(gpu_path, gpu)
    monkeypatch.setattr(verify, "lookup_group", lambda *a: None)
    calls = []
    monkeypatch.setattr(
        verify.ProfileGuard,
        "verify_group_absent",
        lambda self, name, id: calls.append((name, id)),
    )
    result = verify.verify(ledger, gpu_path, tmp_path / "proof")
    assert calls == [("live", "uuid")]
    assert result["mode"] == "cpu_gpu"


@pytest.mark.parametrize("status", ["terminated", "failed", "error", "running"])
def test_exact_sandbox_get_has15second_timeout_and_accepts_only_terminated(
    monkeypatch, status
):
    from chalkcompute import _sandbox as sm

    calls = []
    client = NS(
        _address="https://unit.invalid",
        _headers={},
        close=lambda: calls.append("closed"),
    )
    monkeypatch.setattr(sm.SandboxClient, "from_env", lambda: client)

    def get(request, **kwargs):
        assert request.id == "owned"
        assert kwargs == {"headers": {}, "timeout_ms": 15000}
        return NS(
            sandbox=NS(
                id="owned",
                status=getattr(
                    sm.pb.SandboxStatus, status.upper(), sm.pb.SandboxStatus.UNSPECIFIED
                ),
            )
        )

    def make(*args, **kwargs):
        assert kwargs["interceptors"] == ()
        return NS(get_sandbox=get)

    monkeypatch.setattr(sm.pb_connect, "SandboxServiceClientSync", make)
    if status == "terminated":
        assert verify.sandbox_status("owned") == {"id": "owned", "status": "terminated"}
    else:
        with pytest.raises(RuntimeError, match="TERMINATED/NOT_FOUND"):
            verify.sandbox_status("owned")
    assert calls == ["closed"]


def test_exact_sandbox_get_not_found_is_release_and_other_rpc_errors_propagate(
    monkeypatch,
):
    from chalkcompute import _sandbox as sm
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError

    client = NS(_address="https://unit.invalid", _headers={}, close=lambda: None)
    monkeypatch.setattr(sm.SandboxClient, "from_env", lambda: client)

    def absent(*args, **kwargs):
        raise ConnectError(Code.NOT_FOUND, "absent")

    monkeypatch.setattr(
        sm.pb_connect,
        "SandboxServiceClientSync",
        lambda *a, **k: NS(get_sandbox=absent),
    )
    assert verify.sandbox_status("owned") == {"id": "owned", "status": "not_found"}

    def timeout(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(
        sm.pb_connect,
        "SandboxServiceClientSync",
        lambda *a, **k: NS(get_sandbox=timeout),
    )
    with pytest.raises(TimeoutError):
        verify.sandbox_status("owned")
