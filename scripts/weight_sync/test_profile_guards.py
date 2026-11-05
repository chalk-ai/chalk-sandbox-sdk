"""Failure, ownership and finite-telemetry gates; no infrastructure is contacted."""

import copy
import errno
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import profile_fixture as fixture
import profile_plan as plan
import profile_volumes as volumes
import profile_writers as writers
from profile_gpu_guard import owned_runtime
from profile_gpu_payload import require_zero_failures, validate_profile_events


def test_sparse_memory_accounts_for_dense_boundaries_and_full_nan_expansion():
    def sparse(n):
        return NS(shape=(1000,), nnz=n, encoding="sparse")

    dense = NS(shape=(9000,), nnz=9000, encoding="dense")
    result = fixture.sparse_memory_estimate(
        [sparse(10), sparse(10), dense, sparse(10)], batch_bytes=150
    )
    assert result["sparse_batches"] == 3
    assert result["maximum_sparse_staged_bytes"] == 100
    assert result["maximum_sparse_expanded_nan_bytes"] == 2000
    assert result["largest_tensor_bytes"] == 18000
    assert result["fits_80gib_with_reserve"]
    assert not fixture.sparse_memory_estimate(
        [NS(shape=(40 * 1024**3,), nnz=1, encoding="sparse")]
    )["fits_80gib_with_reserve"]


@pytest.mark.parametrize("error", [errno.ENOSPC, errno.EDQUOT, errno.EOPNOTSUPP])
def test_guest_quota_failure_aborts_and_releases_probe(tmp_path, monkeypatch, error):
    monkeypatch.setattr(
        os,
        "posix_fallocate",
        lambda *a: (_ for _ in ()).throw(OSError(error, "quota")),
        raising=False,
    )
    with pytest.raises(OSError):
        fixture.check_workspace(tmp_path, 1024)
    assert not (tmp_path / "workspace-quota-probe").exists()


def test_guest_quota_rejects_sparse_reservation(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "posix_fallocate", lambda *a: None, raising=False)
    with pytest.raises(RuntimeError, match="sparse"):
        fixture.check_workspace(tmp_path, 1024)
    assert not (tmp_path / "workspace-quota-probe").exists()


def test_guest_quota_proof_is_actual_allocated_capacity(tmp_path, monkeypatch):
    # Portable fake reservation rather than claiming large local space.
    monkeypatch.setattr(
        os,
        "posix_fallocate",
        lambda fd, offset, length: os.write(fd, b"x" * length),
        raising=False,
    )
    result = fixture.check_workspace(tmp_path, 1024)
    assert result["quota_reservation_bytes_verified"] >= 1024
    assert result["quota_reservation_released"]


@pytest.fixture
def frozen(tmp_path):
    runtime = tmp_path / "runtime/chalkcompute"
    (runtime / "weights/serving").mkdir(parents=True)
    for name in (
        "_weights.py",
        "_weights_codec.py",
        "_weights_format.py",
        "weights/serving/profiling.py",
    ):
        (runtime / name).write_text("# frozen source\n")
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    for name in plan.HELPERS:
        (helpers / name).write_text("# frozen helper\n")
    tag = "us-docker.pkg.dev/chalk-develop/compute/compute-demo:abc"
    digest = "sha256:" + "a" * 64
    image = tmp_path / "image.json"
    plan.write_json(image, {"image_uri": tag, "schema_version": 1, "volumes": []})
    provenance = tmp_path / "provenance.json"
    plan.write_json(
        provenance,
        {
            "source_tag": tag,
            "image_uri": tag.rsplit(":", 1)[0] + "@" + digest,
            "resolved_digest": digest,
        },
    )
    out = tmp_path / "frozen"
    plan.prepare(out, runtime.parent, helpers, image, provenance, runtime.parent)
    return out


def test_freezes_every_executable_and_accepts_exact_authorization(frozen, monkeypatch):
    monkeypatch.setattr(plan, "activate_controller_sdk", lambda _: None)
    data = plan.validate(frozen)
    assert set(data["guard_sources"]) == set(plan.OWN_SOURCES + plan.HELPERS)
    assert "profile_writers.py" in data["guard_sources"]
    auth = {
        "approved_by": "parent",
        "action": "execute_profile_cpu",
        "plan_sha256": plan.sha(frozen / "plan.json"),
        "source_policy": "frozen-plan-only",
    }
    assert plan.require_authorization(frozen, auth, auth["action"]) == data
    auth["action"] = "execute_profile_gpu"
    with pytest.raises(ValueError, match="authorization"):
        plan.require_authorization(frozen, auth, "execute_profile_cpu")


@pytest.mark.parametrize(
    "name",
    [
        "guard/profile_writers.py",
        "guard/profile_rpc.py",
        "runtime.tar.gz",
        "image.json",
        "image-provenance.json",
    ],
)
def test_frozen_source_and_image_drift_rejected(frozen, name):
    with (frozen / name).open("ab") as f:
        f.write(b" ")
    with pytest.raises(ValueError):
        plan.validate(frozen)


def test_provenance_content_digest_must_match_even_if_catalog_rehashed(frozen):
    p = json.loads((frozen / "image-provenance.json").read_bytes())
    p["resolved_digest"] = "sha256:" + "b" * 64
    plan.write_json(frozen / "image-provenance.json", p)
    catalog = json.loads((frozen / "plan.json").read_bytes())
    catalog["image_provenance_sha256"] = plan.sha(frozen / "image-provenance.json")
    plan.write_json(frozen / "plan.json", catalog)
    with pytest.raises(ValueError, match="content identity"):
        plan.validate(frozen)


def marker(name, status="inflight"):
    return {
        "phase_id": "cpu",
        "name": name,
        "status": status,
        "started_at": 100,
        "rpc_deadline_epoch": 130,
        "reconcile_until_epoch": 190,
    }


def record():
    return {
        "cpu_phase_id": "cpu",
        "owned_volume_names": ["weight-profile-abc", "weight-profile-abc-reference"],
        "cpu_compute_released": True,
        "host_runtime_verified_absent": True,
    }


def test_lost_create_response_reconciles_late_metadata_after_first_absence(
    tmp_path, monkeypatch
):
    value = record()
    name = value["owned_volume_names"][0]
    plan.write_json(volumes.marker_path(tmp_path, name), marker(name))
    now = [100]
    monkeypatch.setattr(volumes.time, "time", lambda: now[0])
    monkeypatch.setattr(volumes.time, "sleep", lambda n: now.__setitem__(0, now[0] + n))
    calls = []
    volumes.release_created(
        value, tmp_path, lambda r: calls.append((now[0], list(r["owned_volume_names"])))
    )
    assert calls[0] == (100, [name])
    assert calls[-1][0] >= 190
    assert len(calls) > 1  # First NotFound/absence never finalizes ambiguous create.
    assert value["owned_volume_names"] == [name]


@pytest.mark.parametrize("mode", ["no_marker", "collision"])
def test_never_deletes_preexisting_name_without_create_ownership(tmp_path, mode):
    value = record()
    if mode == "collision":
        name = value["owned_volume_names"][0]
        plan.write_json(volumes.marker_path(tmp_path, name), marker(name, "collision"))
    calls = []
    volumes.release_created(
        value, tmp_path, lambda r: calls.extend(r["owned_volume_names"])
    )
    assert calls == []


@pytest.mark.parametrize(
    "field", ["cpu_compute_released", "host_runtime_verified_absent"]
)
def test_volume_cleanup_requires_writer_and_runtime_release(tmp_path, field):
    value = record()
    value[field] = False
    with pytest.raises(RuntimeError, match="writer"):
        volumes.release_created(
            value, tmp_path, lambda r: pytest.fail("delete before writer stop")
        )


def test_create_marker_written_before_rpc_and_lost_response_never_replayed(
    tmp_path, monkeypatch
):
    value = record()
    name = value["owned_volume_names"][0]
    monkeypatch.setattr(volumes, "require_cpu_guard", lambda *a: None)

    def lost(timeout):
        assert timeout == 30
        assert (
            json.loads(volumes.marker_path(tmp_path, name).read_bytes())["status"]
            == "inflight"
        )
        raise TimeoutError

    with pytest.raises(TimeoutError):
        volumes.create_owned(value, tmp_path, name, lost)
    with pytest.raises(RuntimeError, match="replay"):
        volumes.create_owned(value, tmp_path, name, lambda *a: pytest.fail("replayed"))


def test_wrong_writer_identity_never_signals(tmp_path, monkeypatch):
    known = {"pid": 123, "group": 123, "started": "old"}
    monkeypatch.setattr(
        writers, "process_identity", lambda pid: dict(known, started="reused")
    )
    monkeypatch.setattr(os, "killpg", lambda *a: pytest.fail("unrelated signal"))
    with pytest.raises(RuntimeError, match="identity changed"):
        writers.stop_writer({"pin_writer": known}, tmp_path)


def test_inert_pin_child_expires_without_sdk_import_or_write(tmp_path):
    request = {
        "phase_id": "phase",
        "parent_pid": os.getpid(),
        "deadline": time.time() + 0.4,
    }
    plan.write_json(tmp_path / "request.json", request)
    result = subprocess.run(
        [sys.executable, str(Path(writers.__file__)), "--child", str(tmp_path)],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode
    assert (tmp_path / "ready.json").exists()
    assert not (tmp_path / "result.json").exists()
    assert b"Inert PIN child expired" in result.stderr


def test_inert_pin_child_rejects_wrong_phase_before_sdk_import(tmp_path):
    plan.write_json(
        tmp_path / "request.json",
        {"phase_id": "phase", "parent_pid": os.getpid(), "deadline": time.time() + 5},
    )
    plan.write_json(
        tmp_path / "authorization.json", {"phase_id": "wrong", "identity": {}}
    )
    result = subprocess.run(
        [sys.executable, str(Path(writers.__file__)), "--child", str(tmp_path)],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode
    assert b"authorization identity/phase differs" in result.stderr
    assert not (tmp_path / "result.json").exists()


def events():
    host = dict.fromkeys(
        (
            "manifest",
            "verify",
            "read_checksum",
            "decode",
            "h2d_host",
            "dense_load_host",
            "sparse_load_host",
            "synchronize_host",
        ),
        1.0,
    )
    result = []
    for version in (1, 0):
        worker = {
            "schema_version": 1,
            "version": version,
            "rank": 0,
            "local_rank": 0,
            "worker_pid": 123,
            "device": "cuda:0",
            "host_ms": host,
            "stream_elapsed_ms": {"sparse_load_host": 10.0},
            "counts": {
                "payload_bytes": 1,
                "value_bytes": 1,
                "index_bytes": 1,
                "tensors": 399,
                "sparse_batches": 1,
            },
        }
        base = {"event": "chalk_weight_sync_profile", "version": version}
        result.extend(
            [
                dict(
                    base, phase="apply", workers=[worker], collective_rpc_host_ms=12.0
                ),
                dict(base, phase="cache_reset", host_ms=1),
                dict(
                    base,
                    phase="sync",
                    from_version=1 - version,
                    reload_host_ms=1,
                    verify_host_ms=1,
                    apply_ms=12,
                    pause_ms=13,
                    drain_host_ms=1,
                ),
            ]
        )
    return copy.deepcopy(result)


def test_target_and_restore_require_all_worker_adapter_and_gate_profiles():
    assert set(validate_profile_events(events())) == {"0", "1"}
    with pytest.raises(ValueError, match="Missing or repeated"):
        validate_profile_events(events()[:-1])


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True])
def test_nonfinite_or_invalid_timing_rejected(bad):
    value = events()
    value[0]["workers"][0]["host_ms"]["read_checksum"] = bad
    with pytest.raises(ValueError, match="timings"):
        validate_profile_events(value)


@pytest.mark.parametrize(
    "change", [{"rank": 1}, {"device": "cpu"}, {"version": 2}, {"worker_pid": 0}]
)
def test_wrong_native_worker_identity_rejected(change):
    value = events()
    value[0]["workers"][0].update(change)
    with pytest.raises(ValueError):
        validate_profile_events(value)


@pytest.mark.parametrize(
    "text",
    [b"", b"chalk_weight_sync_failures 1\n", b"chalk_weight_sync_failures nan\n"],
)
def test_missing_or_nonzero_failure_counter_rejected(text):
    with pytest.raises(RuntimeError, match="counter"):
        require_zero_failures(NS(call=lambda *a, **k: NS(status=200, content=text)))


def test_exact_group_uuid_ownership_predicate():
    group_id = "11111111-1111-1111-1111-111111111111"
    replica = "sg-" + group_id + "-replica"
    assert owned_runtime(replica, group_id, [replica])
    assert not owned_runtime("sg-unrelated-replica", group_id, [replica])
    with pytest.raises(ValueError):
        owned_runtime(replica, group_id, ["sg-other-replica"])


def rpc_guard(tmp_path):
    value = {"data_transfer_verified": True, "gpu_deadline_epoch": time.time() + 60}
    guard = NS(record=value, out=tmp_path, check_budget=lambda: None)
    guard.save = lambda: plan.write_json(tmp_path / "launcher.json", value)
    return guard


def test_gpu_create_marks_inflight_before_single_rpc_and_retains_lost_response(
    tmp_path, monkeypatch
):
    from chalkcompute._connect import ConnectClient
    from profile_rpc import make_client

    guard = rpc_guard(tmp_path)
    calls = []

    def create_scaling_group(request):
        pass

    def lost(self, method, request, **kwargs):
        calls.append(kwargs)
        assert json.loads((tmp_path / "launcher.json").read_bytes())[
            "submission_inflight"
        ]["create_scaling_group"]
        raise TimeoutError

    monkeypatch.setattr(ConnectClient, "call", lost)
    client = make_client(guard)
    with pytest.raises(TimeoutError):
        client.call(create_scaling_group, create_request())
    assert calls == [{"timeout": 30}]
    with pytest.raises(RuntimeError, match="replay"):
        client.call(create_scaling_group, create_request())
    assert len(calls) == 1


@pytest.mark.parametrize(
    "proof", ["no_transfer", "previous_native_active", "ledger_lost"]
)
def test_gpu_claim_refuses_untransferred_data_overlapping_gpu_or_lost_ledger(
    tmp_path, monkeypatch, proof
):
    from chalkcompute._connect import ConnectClient
    from profile_rpc import make_client

    guard = rpc_guard(tmp_path)
    if proof == "no_transfer":
        guard.record["data_transfer_verified"] = False
    elif proof == "previous_native_active":
        guard.record["group_ids"] = {"live": "uuid"}
        guard.record["released_groups"] = ["uuid"]  # API deletion alone insufficient.
    else:
        guard.save = lambda: None
        plan.write_json(tmp_path / "launcher.json", {})

    def create_scaling_group(request):
        pass

    monkeypatch.setattr(
        ConnectClient, "call", lambda *a, **k: pytest.fail("unpermitted claim")
    )
    with pytest.raises(RuntimeError):
        make_client(guard).call(create_scaling_group, create_request())


def test_read_only_gpu_client_refuses_compute_create():
    from profile_rpc import make_client

    def create_scaling_group(request):
        pass

    with pytest.raises(RuntimeError, match="Read-only"):
        make_client().call(create_scaling_group, create_request())


def test_group_owner_retained_after_api_cr_disappears(tmp_path, monkeypatch):
    import profile_gpu_guard as module

    group_id = "11111111-1111-1111-1111-111111111111"
    record = {
        "expected_namespaces": ["qa"],
        "kube_contexts": ["ctx"],
        "profile_group_resources": {
            "live": {
                "id": group_id,
                "owners": ["owner-uid"],
                "replicas": ["sg-" + group_id + "-replica"],
                "replica_uids": ["replica-uid"],
                "context": "ctx",
                "namespace": "qa",
            }
        },
    }
    guard = module.ProfileGuard("live", tmp_path, record, "ctx", "ctx")
    monkeypatch.setattr(guard, "kube_items", lambda *a, **k: [])
    calls = []
    monkeypatch.setattr(
        module,
        "native_absence",
        lambda g, r: calls.append((g, r)) or {"runsc_absent": True},
    )
    guard.verify_group_absent("live", group_id)
    assert calls == [(group_id, ["sg-" + group_id + "-replica"])]
    assert group_id in record["profile_native_release_proofs"]


def test_remaining_pod_from_retained_replica_uid_blocks_native_release(
    tmp_path, monkeypatch
):
    import profile_gpu_guard as module

    group_id = "11111111-1111-1111-1111-111111111111"
    record = {
        "expected_namespaces": ["qa"],
        "kube_contexts": ["ctx"],
        "profile_group_resources": {
            "live": {
                "id": group_id,
                "owners": ["owner"],
                "replicas": [],
                "replica_uids": ["child"],
                "context": "ctx",
                "namespace": "qa",
            }
        },
    }
    guard = module.ProfileGuard("live", tmp_path, record, "ctx", "ctx")
    monkeypatch.setattr(
        guard,
        "kube_items",
        lambda kind, **k: (
            [{"metadata": {"ownerReferences": [{"uid": "child"}]}}]
            if kind == "pods"
            else []
        ),
    )
    monkeypatch.setattr(
        module, "native_absence", lambda *a: pytest.fail("premature native proof")
    )
    with pytest.raises(RuntimeError, match="pods"):
        guard.verify_group_absent("live", group_id)


def test_watchdog_parent_death_stops_writer_compute_then_data_and_recovers_transient_error(
    tmp_path, monkeypatch
):
    import profile_gpu_guard as module

    now = [100.0]
    monkeypatch.setattr(module.time, "time", lambda: now[0])
    monkeypatch.setattr(module.time, "sleep", lambda n: now.__setitem__(0, now[0] + n))
    monkeypatch.setattr(
        module, "make_client", lambda: NS(_env_id="qa", _ensure_auth=lambda: None)
    )
    monkeypatch.setattr(module.host, "runtime_absence", lambda _: {})
    monkeypatch.setattr(module, "lookup_group", lambda *a: None)
    monkeypatch.setattr(
        module.os, "kill", lambda *a: (_ for _ in ()).throw(ProcessLookupError())
    )
    order = []
    monkeypatch.setattr(module, "stop_writer", lambda *a: order.append("writer"))
    calls = [0]

    class Guard:
        def __init__(self, name, out, record, *args):
            self.name, self.out, self.record, self.groups = name, out, record, []

        def cleanup_compute(self):
            order.append("compute")
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError("transient observation")

        def save(self):
            pass

    monkeypatch.setattr(module, "ProfileGuard", Guard)
    monkeypatch.setattr(module, "cleanup_data", lambda g: order.append("data") or [])
    ledger = tmp_path / "launcher.json"
    plan.write_json(
        ledger,
        {
            "environment_id": "qa",
            "gpu_phase_id": "phase",
            "name": "weight-profile-abc",
            "kube_context": "ctx",
            "fallback_kube_context": "ctx",
        },
    )
    module.watch(ledger, 1000, 99999999)
    assert order == ["writer", "compute", "writer", "compute", "data"]
    assert json.loads((tmp_path / "watchdog-exited.json").read_bytes())["cleaned"]
    history = json.loads(
        (tmp_path / "watchdog-cleanup/cleanup-history.json").read_bytes()
    )
    assert history[0]["errors"] == ["RuntimeError"]
    assert history[1]["errors"] == []


def create_request():
    from chalkcompute._gen.chalk.container.v1 import service_pb as cp
    from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sp

    return sp.CreateScalingGroupRequest(
        spec=sp.ScalingGroupSpec(
            container_spec=cp.ChalkContainerSpec(compute_class=cp.ComputeClass.HOST)
        )
    )


def test_scoped_wire_request_pins_exact_host_pool_and_runtime():
    from profile_rpc import pin_placement
    from chalkcompute._gen.chalk.container.v1 import service_pb as cp

    request = create_request()
    pin_placement(request)
    spec = request.spec.container_spec
    assert spec.runtime == cp.Runtime.GVISOR
    assert spec.placement.value.host_pool_id == "ExternalClusterHosts"
