from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts" / "sglang"))
gpu = importlib.import_module("gpu_smoke")
scenario = importlib.import_module("gpu_payload")
watch = importlib.import_module("gpu_guard")


@pytest.fixture
def prepared(tmp_path):
    source = tmp_path / "sdk"
    package = source / "chalkcompute"
    package.mkdir(parents=True)
    for name in ("__init__.py", "_weights_codec.py", "_weights_format.py"):
        (package / name).write_text("# reviewed\n")
    serving = package / "weights" / "serving"
    serving.mkdir(parents=True)
    (serving / "__init__.py").write_text("# reviewed runtime\n")
    guards = tmp_path / "guards"
    guards.mkdir()
    for name in gpu.base.GUARD_FILES:
        (guards / name).write_text("# lifecycle\n")
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    for name in gpu.payload.SOURCE_FILES.values():
        (upstream / name).write_text("# upstream\n")
    cpu = tmp_path / "cpu"
    cpu.mkdir()
    bundle = b"verified small fixture"
    (cpu / "fixture.tar.gz").write_bytes(bundle)
    evidence = {
        "launcher.json": {
            "status": "released",
            "preflight_status": "passed",
            "host_runtime_verified_absent": True,
        },
        "cpu-proof.json": {
            "bundle": {
                "bytes": len(bundle),
                "sha256": hashlib.sha256(bundle).hexdigest(),
            }
        },
        "independent-host-cache-proof.json": {
            "image": gpu.payload.IMAGE,
            "hypervisor_exact_digest_cache_entries": [
                {"complete_marker": True, "mounted_rootfs": True}
            ],
        },
        "independent-cleanup-proof.json": {
            "native_host": {"runsc_absent": True, "containerd_task_absent": True},
            "cr_and_owner_uid_pod_checks": [{"owned_count": 0}],
        },
    }
    evidence["cpu-proof.json"].update(
        image=gpu.payload.IMAGE,
        source_sha256={
            relative: gpu.payload.digest(upstream / filename)
            for relative, filename in gpu.payload.SOURCE_FILES.items()
        },
    )
    evidence["plan.json"] = {"overlay_files": gpu.base.overlay_archive(source)[1]}
    for name, value in evidence.items():
        (cpu / name).write_text(json.dumps(value))
    evidence["launcher.json"].update(
        proof_sha256=gpu.payload.digest(cpu / "cpu-proof.json"),
        plan_sha256=gpu.payload.digest(cpu / "plan.json"),
        fixture_sha256=gpu.payload.digest(cpu / "fixture.tar.gz"),
    )
    (cpu / "launcher.json").write_text(json.dumps(evidence["launcher.json"]))
    out = tmp_path / "plan"
    plan = gpu.prepare(out, source, guards, upstream, cpu)
    return out, plan


def test_plan_preparation_and_validation_claim_nothing(prepared):
    out, plan = prepared
    assert gpu.validate(out) == plan
    assert plan["gpu"] == "nvidia-a100-80gb:1"
    assert plan["chalk_identity"] is False
    assert plan["lease_seconds"] == 900
    assert plan["cleanup_reserve_seconds"] == 120
    assert not (out / "launcher.json").exists()
    assert plan["tolerance"] == 1e-3 and plan["minimum_change"] == 0.01


@pytest.mark.parametrize(
    "key,value",
    [
        ("gpu", "amd-mi355x:1"),
        ("lease_seconds", 901),
        ("chalk_identity", True),
        ("tolerance", 0.01),
        ("cleanup_reserve_seconds", 60),
    ],
)
def test_fixed_contract_rejects_changed_device_budget_identity_tolerance(
    prepared, key, value
):
    out, plan = prepared
    plan[key] = value
    gpu.base.write_json(out / "plan.json", plan)
    with pytest.raises(ValueError, match="contract"):
        gpu.validate(out)


@pytest.mark.parametrize(
    "name",
    [
        "gpu_smoke.py",
        "gpu_payload.py",
        "gpu_guard.py",
        "host_guard.py",
        "fixture.tar.gz",
        "cpu-proof.json",
        "independent-cleanup-proof.json",
        "overlay.tar.gz",
        "guard/cpu_seed.py",
    ],
)
def test_every_execution_source_and_evidence_is_bound(prepared, name):
    out, _ = prepared
    (out / name).write_bytes(b"changed")
    with pytest.raises(ValueError):
        gpu.validate(out)


def test_gpu_constructor_explicit_identity_and_immutable_mount():
    recorded = {}

    def sandbox(**kwargs):
        recorded.update(kwargs)
        return recorded

    fake = SimpleNamespace(
        Sandbox=sandbox,
        VolumeMount=lambda **kwargs: kwargs,
        RestartPolicy=SimpleNamespace(NEVER="never"),
        ComputeClass=SimpleNamespace(HOST="host"),
        Runtime=SimpleNamespace(GVISOR="gvisor"),
        ExecutionBackend=SimpleNamespace(SERVERLESS="serverless"),
    )
    result = gpu.make_sandbox(
        fake,
        {"seed_name": "owned", "cpu_deadline_epoch": time.time() + 900},
        {"image": gpu.payload.IMAGE},
        "one-shot",
        {"name": "owned-fixture", "version_id": 42},
    )
    assert result["chalk_identity"] is False and result["gpu"] == "nvidia-a100-80gb:1"
    assert result["volumes"] == [
        {"name": "owned-fixture", "mount_path": "/fixture", "version_id": 42}
    ]
    assert result["restart_policy"] == "never" and "HF_HUB_OFFLINE" in result["env"]
    assert not any(key.startswith("CHALK_") for key in result["env"])


def test_guard_checked_before_upload_metadata(tmp_path, monkeypatch):
    phase = "fixed-phase"
    gpu.base.write_json(tmp_path / "launcher.json", {"cpu_phase_id": phase})
    gpu.base.write_json(
        tmp_path / "upload-authorized.json", {"pid": os.getpid(), "phase_id": phase}
    )

    def reject(*args):
        raise RuntimeError("guard missing before metadata")

    monkeypatch.setattr(gpu, "require_guard", reject)
    with pytest.raises(RuntimeError, match="before metadata"):
        gpu.upload(tmp_path)
    assert gpu.base.read_json(tmp_path / "upload-ready.json")["phase_id"] == phase


def test_guard_reserves_cleanup_and_binds_watchdog(tmp_path, monkeypatch):
    record = {
        "cpu_deadline_epoch": time.time() + 900,
        "cpu_phase_id": "owned",
        "cpu_watchdog_pid": 10,
    }
    gpu.base.write_json(
        tmp_path / "cpu-watchdog-ready.json", {"pid": 10, "phase_id": "different"}
    )
    monkeypatch.setattr(
        os, "kill", lambda *args: pytest.fail("must reject before PID use")
    )
    with pytest.raises(RuntimeError, match="mismatch"):
        gpu.require_guard(record, tmp_path)
    record["cpu_deadline_epoch"] = time.time() + 100
    with pytest.raises(TimeoutError, match="cleanup reserve"):
        gpu.require_guard(record, tmp_path)


def sample(value=0.0):
    return [
        {
            "text": "v4",
            "logprobs": {"tokens": ["v4"], "top_logprobs": [{"v4": value, "v5": -1.0}]},
        }
    ]


def test_oracle_strict_tolerance_tokens_and_nonfinite():
    assert scenario.compare(sample(), sample()) == 0
    assert scenario.compare(sample(0.0009), sample()) == pytest.approx(0.0009)
    for wrong in (sample(0.0011), sample(float("nan"))):
        with pytest.raises(AssertionError):
            scenario.compare(wrong, sample())
    wrong = sample()
    wrong[0]["text"] = "v5"
    with pytest.raises(AssertionError, match="Greedy"):
        scenario.compare(wrong, sample())


def test_measurable_native_change_does_not_widen_threshold():
    with pytest.raises(AssertionError, match="measurable"):
        scenario.measurable_change(sample(), sample(0.009))
    assert scenario.measurable_change(sample(), sample(0.011))[
        "maximum_common_logprob_change"
    ] == pytest.approx(0.011)


def test_upload_cleanup_refuses_pid_reuse(tmp_path, monkeypatch):
    known = {"pid": 123, "group": 123, "started": "old"}
    monkeypatch.setattr(
        watch, "process_identity", lambda pid: {**known, "started": "new"}
    )
    monkeypatch.setattr(
        os,
        "killpg",
        lambda *args: pytest.fail("unrelated process must not be signaled"),
    )
    with pytest.raises(RuntimeError, match="identity changed"):
        watch.stop_upload({"upload_writer": known}, tmp_path)


def test_native_group_signal_refuses_reused_leader(monkeypatch):
    known = {"pid": 123, "start_ticks": "1"}
    monkeypatch.setattr(
        scenario, "identity", lambda pid: {"pid": pid, "start_ticks": "2"}
    )
    monkeypatch.setattr(
        os, "killpg", lambda *args: pytest.fail("reused process must not be signaled")
    )
    with pytest.raises(RuntimeError, match="identity changed"):
        scenario.signal_group(known, 15)


def test_guest_program_compiles_and_carries_audit_and_offline_inputs():
    code = gpu.guest_code()
    compile(code, "bounded-gpu-guest", "exec")
    assert "CONFIG['cpu_payload']" in code and "CONFIG['fixture_sha256']" in code
    assert "verify_sources" in code and "verify_native_schemas" in code
    assert "os.environ['PYTHONPATH']" in code


def test_smoke_uses_real_supervisor_routes_headers_and_completion_contract():
    import asyncio

    async def check():
        import aiohttp
        from aiohttp import web
        from aiohttp.test_utils import TestServer
        from chalkcompute.weights.serving.supervisor import AdmissionGate, create_app

        async def completion_response(request):
            body = await request.json()
            assert body["model"] == "tiny-fixture"
            return web.json_response({"choices": [sample()[0]]})

        async def models(request):
            return web.json_response({"data": [{"id": "tiny-fixture"}]})

        engine = web.Application()
        engine.router.add_post("/v1/completions", completion_response)
        engine.router.add_get("/v1/models", models)
        async with TestServer(engine) as upstream:
            gate = AdmissionGate()
            gate.version = 7
            async with TestServer(
                create_app(gate, str(upstream.make_url("")))
            ) as proxy:
                port = proxy.port
                async with aiohttp.ClientSession() as client:
                    health, headers = await scenario.request(
                        client, scenario.HEALTH_ROUTE, port=port
                    )
                    assert health == {"applied_version": 7, "healthy": True}
                    assert headers["x-chalk-weight-version"] == "7"
                    await scenario.wait_version(
                        client, SimpleNamespace(budget=lambda maximum: 1), 7, port=port
                    )
                    results = await scenario.samples(client, port=port, version=7)
                    assert len(results) == len(scenario.PROMPTS)
                    assert scenario.POLICY_HEADER == "x-chalk-policy-version"
                    async with client.get(
                        str(proxy.make_url(scenario.METRICS_ROUTE))
                    ) as response:
                        assert response.status == 200
                        assert "chalk_weight_sync_updates 0" in await response.text()
                    for wrong in ("/health", "/metrics"):
                        async with client.get(str(proxy.make_url(wrong))) as response:
                            assert response.status == 404

    asyncio.run(check())


def test_native_group_cleanup_includes_orphans_after_leader_exit(monkeypatch):
    known = {"pid": 123, "start_ticks": "1"}
    child = {"pid": 124, "start_ticks": "2", "group": 123, "session": 123, "state": "S"}
    monkeypatch.setattr(scenario, "identity", lambda pid: child if pid == 124 else None)
    monkeypatch.setattr(
        scenario,
        "Path",
        lambda path: SimpleNamespace(iterdir=lambda: [SimpleNamespace(name="124")]),
    )
    calls = []
    monkeypatch.setattr(os, "killpg", lambda *args: calls.append(args))
    scenario.signal_group(known, 15)
    assert calls == [(123, 15)]


def test_upload_writer_is_stopped_before_volume_delete(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        watch.host_guard,
        "install_release_check",
        lambda guard: calls.append("native-wrapper"),
    )
    monkeypatch.setattr(
        watch, "stop_upload", lambda record, out: calls.append("writer-joined")
    )
    guard = SimpleNamespace(release_data=lambda record: calls.append("volume-delete"))
    watch.install(guard, tmp_path)
    guard.release_data({})
    assert calls == ["native-wrapper", "writer-joined", "volume-delete"]


def test_token_flip_alone_does_not_bypass_change_threshold():
    current = sample(0.001)
    current[0]["text"] = "different"
    with pytest.raises(AssertionError, match="measurable"):
        scenario.measurable_change(sample(), current)


def test_lost_volume_create_retains_finite_inflight_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu, "require_guard", lambda *args: None)
    record = {"cpu_phase_id": "phase", "owned_volume_names": ["owned-fixture"]}

    def lost(timeout):
        assert timeout == 30
        marker = gpu.base.read_json(tmp_path / "upload-create.json")
        assert marker["status"] == "inflight"
        raise TimeoutError("response lost after server received create")

    with pytest.raises(TimeoutError):
        gpu.create_fixture_volume(record, tmp_path, lost)
    marker = gpu.base.read_json(tmp_path / "upload-create.json")
    assert marker["status"] == "inflight"
    assert marker["rpc_deadline_epoch"] - marker["started_at"] == 30
    assert marker["reconcile_until_epoch"] - marker["started_at"] == 90


def test_ambiguous_create_reconciles_late_volume_before_absence(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(watch.time, "time", lambda: clock[0])
    monkeypatch.setattr(
        watch.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    record = {"cpu_phase_id": "phase", "owned_volume_names": ["owned-fixture"]}
    gpu.base.write_json(
        tmp_path / "upload-create.json",
        {
            "phase_id": "phase",
            "name": "owned-fixture",
            "status": "inflight",
            "started_at": 0,
            "rpc_deadline_epoch": 30,
            "reconcile_until_epoch": 90,
        },
    )
    checks = []

    def release(current):
        checks.append({"at": clock[0], "late_volume_found_and_deleted": clock[0] == 40})
        current["volume_verified_absent"] = True

    guard = SimpleNamespace(write_json=gpu.base.write_json)
    watch.release_fixture_data(guard, record, tmp_path, release)
    assert checks[0]["at"] == 0 and checks[-1]["at"] == 90
    assert any(item["late_volume_found_and_deleted"] for item in checks)
    assert record["volume_verified_absent"] is True
    assert record["fixture_create_reconciled_until_epoch"] == 90


def test_absent_create_marker_never_deletes_preexisting_collision(tmp_path):
    record = {"owned_volume_names": ["possibly-preexisting"]}
    observed = []
    watch.release_fixture_data(
        SimpleNamespace(),
        record,
        tmp_path,
        lambda current: observed.append(current["owned_volume_names"]),
    )
    assert observed == [[]]
