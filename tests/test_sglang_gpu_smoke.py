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


def test_short_cache_diagnostic_plan_is_fixed_and_distinct(prepared):
    out, _ = prepared
    root = out.parent
    target = root / "diagnostic-plan"
    plan = gpu.prepare(
        target,
        root / "sdk",
        root / "guards",
        root / "upstream",
        root / "cpu",
        cache_diagnostic=True,
    )
    assert gpu.validate(target) == plan
    assert plan["kind"] == "sglang_cache_diagnostic_plan"
    assert plan["lease_seconds"] == 600 and plan["cleanup_reserve_seconds"] == 120
    assert plan["scenario"] == "native_cache_diagnostic"
    assert plan["gpu"] == "nvidia-a100-80gb:1" and plan["tolerance"] == 1e-3


def test_comparison_saves_all_raw_prompts_and_max_location_before_raising(tmp_path):
    actual = sample(0.01) + sample(0.02)
    reference = sample() + sample()
    with pytest.raises(scenario.NativeComparisonError) as raised:
        scenario.compare(actual, reference, out=tmp_path, label="failed-case")
    report = raised.value.details
    assert report["maximum_logprob_error"] == pytest.approx(0.02)
    assert report["maximum_error_location"]["prompt_index"] == 1
    assert report["maximum_error_location"]["generated_token_index"] == 0
    assert report["maximum_error_location"]["observed"] == 0.02
    directory = tmp_path / "comparisons" / "failed-case"
    assert len(list(directory.glob("prompt-*.json"))) == 2
    assert (
        json.loads((directory / "prompt-00.json").read_text())["observed"] == actual[0]
    )
    assert (
        json.loads((directory / "prompt-01.json").read_text())["reference"]
        == reference[1]
    )
    assert json.loads((directory / "summary.json").read_text()) == report


def test_diagnostic_bundle_retains_raw_cache_and_ack_without_fixture(tmp_path):
    import tarfile

    samples = tmp_path / "samples" / "cold"
    samples.mkdir(parents=True)
    (samples / "prompt-00.json").write_text('{"observed":1}')
    native = tmp_path / "native-01"
    native.mkdir()
    (native / "native.log").write_text("native log")
    (native / "gate-updates.json").write_text("[]")
    (tmp_path / "native-flush-ack.json").write_text('{"status":200}')
    stream = tmp_path / "stream"
    stream.mkdir()
    (stream / "model.safetensors").write_text("fixture")
    proof = scenario.bundle_diagnostics(tmp_path)
    data = (tmp_path / proof["file"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == proof["sha256"]
    with tarfile.open(tmp_path / proof["file"]) as archive:
        assert set(archive.getnames()) == {
            "samples/cold/prompt-00.json",
            "native-01/native.log",
            "native-01/gate-updates.json",
            "native-flush-ack.json",
        }


def test_independent_volume_verifier_rejects_other_environment_before_read(monkeypatch):
    verifier = importlib.import_module("verify_host_release")
    import chalkcompute._connect as connection
    import chalkcompute as cc

    monkeypatch.setattr(
        connection,
        "ConnectClient",
        lambda: SimpleNamespace(_ensure_auth=lambda: None, _env_id="wrong-environment"),
    )
    monkeypatch.setattr(
        cc,
        "Volume",
        lambda *args, **kwargs: pytest.fail("wrong environment must not be queried"),
    )
    with pytest.raises(RuntimeError, match="environment differs"):
        verifier.verify_volumes(
            {
                "environment_id": "correct-environment",
                "owned_volume_names": ["owned-fixture"],
            }
        )


def test_release_verifier_exact_api_lookup_is_bound_and_timed(monkeypatch):
    verifier = importlib.import_module("verify_host_release")
    import chalkcompute._connect as connection
    from chalkcompute._gen.chalk.sandbox.v2 import service_pb

    calls = []
    stub = SimpleNamespace(get_sandbox=object())

    def invoke(method, request, **kwargs):
        calls.append((method, request.id, kwargs))
        return service_pb.GetSandboxResponse(
            sandbox=service_pb.SandboxInfo(
                id="owned-id",
                name="owned-name",
                status=service_pb.SandboxStatus.TERMINATED,
            )
        )

    client = SimpleNamespace(
        _ensure_auth=lambda: None,
        _env_id="env-1",
        stub=lambda cls: stub,
        call=invoke,
    )
    monkeypatch.setattr(connection, "ConnectClient", lambda: client)
    proof = verifier.verify_api_association(
        {
            "environment_id": "env-1",
            "seed_sandbox_id": "owned-id",
            "name": "phase-name",
            "seed_name": "owned-name",
        }
    )
    assert calls == [(stub.get_sandbox, "owned-id", {"timeout": 15})]
    assert proof["status"] == "terminated"
    client._env_id = "wrong-env"
    with pytest.raises(RuntimeError, match="environment differs"):
        verifier.verify_api_association({"environment_id": "env-1"})
    assert len(calls) == 1


@pytest.mark.parametrize("outcome", ["running", "wrong-id", "not-found", "timeout"])
def test_release_verifier_api_rejects_live_mismatched_and_unknown(monkeypatch, outcome):
    verifier = importlib.import_module("verify_host_release")
    import chalkcompute._connect as connection
    from chalkcompute._gen.chalk.sandbox.v2 import service_pb
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError

    def invoke(*args, **kwargs):
        if outcome == "not-found":
            raise ConnectError(Code.NOT_FOUND, "absent")
        if outcome == "timeout":
            raise ConnectError(Code.DEADLINE_EXCEEDED, "unknown")
        return service_pb.GetSandboxResponse(
            sandbox=service_pb.SandboxInfo(
                id="other" if outcome == "wrong-id" else "owned-id",
                name="owned-name",
                status=service_pb.SandboxStatus.RUNNING,
            )
        )

    monkeypatch.setattr(
        connection,
        "ConnectClient",
        lambda: SimpleNamespace(
            _ensure_auth=lambda: None,
            _env_id="env-1",
            stub=lambda cls: SimpleNamespace(get_sandbox=object()),
            call=invoke,
        ),
    )
    record = {
        "environment_id": "env-1",
        "seed_sandbox_id": "owned-id",
        "name": "owned-name",
    }
    if outcome == "not-found":
        assert verifier.verify_api_association(record)["status"] == "not_found"
    else:
        with pytest.raises((RuntimeError, ConnectError)):
            verifier.verify_api_association(record)


@pytest.mark.parametrize("status", ["inflight", "acknowledged"])
def test_release_verifier_checks_ambiguous_fixture_horizon(
    tmp_path, monkeypatch, status
):
    verifier = importlib.import_module("verify_host_release")
    monkeypatch.setattr(verifier.time, "time", lambda: 200)
    marker = {
        "phase_id": "phase-1",
        "name": "owned-fixture",
        "status": status,
        "started_at": 100,
        "rpc_deadline_epoch": 130,
        "reconcile_until_epoch": 190,
    }
    (tmp_path / "upload-create.json").write_text(json.dumps(marker))
    record = {
        "cpu_phase_id": "phase-1",
        "submission_inflight": False,
        "owned_volume_names": ["owned-fixture"],
        "fixture_create_reconciled_until_epoch": 180,
    }
    if status == "inflight":
        with pytest.raises(RuntimeError, match="horizon not satisfied"):
            verifier.verify_reconciliation(record, tmp_path)
    else:
        assert (
            verifier.verify_reconciliation(record, tmp_path)["fixture_create_status"]
            == status
        )
    record["fixture_create_reconciled_until_epoch"] = 195
    assert (
        verifier.verify_reconciliation(record, tmp_path)["fixture_create_status"]
        == status
    )
    record["submission_inflight"] = True
    with pytest.raises(RuntimeError, match="still ambiguous"):
        verifier.verify_reconciliation(record, tmp_path)


def test_release_verifier_rejects_early_sandbox_reconciliation(tmp_path, monkeypatch):
    verifier = importlib.import_module("verify_host_release")
    monkeypatch.setattr(verifier.time, "time", lambda: 200)
    record = {
        "submission_inflight": False,
        "owned_volume_names": [],
        "create_reconcile_until_epoch": 190,
        "create_reconciled_until_epoch": 180,
    }
    with pytest.raises(RuntimeError, match="horizon not satisfied"):
        verifier.verify_reconciliation(record, tmp_path)
    record["create_reconciled_until_epoch"] = 195
    assert (
        verifier.verify_reconciliation(record, tmp_path)[
            "sandbox_reconciled_until_epoch"
        ]
        == 195
    )


@pytest.mark.parametrize("name", ["../other.json", "/tmp/other.json", "proof.txt"])
def test_release_verifier_rejects_unsafe_proof_name_before_reads(tmp_path, name):
    verifier = importlib.import_module("verify_host_release")
    with pytest.raises(ValueError, match="without directories"):
        verifier.verify(tmp_path, proof_name=name)


def test_cache_state_bounds_realistic_full_internal_config(tmp_path, monkeypatch):
    import asyncio

    # Pinned SchedulerInternalStateController returns all resolved server args,
    # topology, memory/debug data and optionally environment vars for each DP.
    internal = {
        **{f"unrelated_arg_{index}": "x" * 1024 for index in range(200)},
        "disable_radix_cache": False,
        "attention_backend": "triton",
        "dtype": "float32",
        "world_size": 1,
        "memory_usage": {"large_debug": list(range(10000))},
        "env_vars": {"DO_NOT_PERSIST": "secret-sentinel"},
    }
    server = {**internal, "internal_states": [internal] * 8}
    model = {"model_path": "v0", "load_format": "auto", "dtype": "float32"}

    async def response(client, path, **kwargs):
        return (server if path == "/server_info" else model), {}

    monkeypatch.setattr(scenario, "request", response)
    asyncio.run(scenario.cache_state(None, tmp_path, "large-config"))
    data = (tmp_path / "cache-state-large-config.json").read_bytes()
    state = json.loads(data)

    assert len(data) < 16384
    assert state["internal_state_count"] == 8
    assert state["internal_states_omitted"] == 4
    assert len(state["internal_states"]) == 4
    assert state["internal_states"][0]["disable_radix_cache"] is False
    assert state["config"]["attention_backend"] == "triton"
    assert state["server_response_bytes"] > 1024 * 1024
    raw = json.dumps(server, sort_keys=True, separators=(",", ":")).encode()
    assert state["server_response_sha256"] == hashlib.sha256(raw).hexdigest()
    assert b"secret-sentinel" not in data
    assert b"large_debug" not in data


def test_cache_diagnostic_finishes_flush_and_fresh_cases_after_warm_failure(
    tmp_path, monkeypatch
):
    import asyncio
    import aiohttp

    events = []

    class Native:
        def __init__(self, *args):
            pass

        async def start(self, model, **kwargs):
            events.append(("start", kwargs))

        async def stop(self):
            events.append(("stop", {}))

    class Response:
        status = 200

        async def text(self):
            return "Cache flushed"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def post(self, url, **kwargs):
            assert url.endswith("/flush_cache")
            events.append(("flush", {}))
            return Response()

    values = iter([0.0, 0.02, 0.03, 0.0, 0.0])

    async def samples(*args, **kwargs):
        events.append(("samples", kwargs["label"]))
        return sample(next(values))

    async def state(*args):
        return {"config": {"disable_radix_cache": False}}

    monkeypatch.setattr(scenario, "Native", Native)
    monkeypatch.setattr(scenario, "samples", samples)
    monkeypatch.setattr(scenario, "cache_state", state)
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kwargs: Client())
    args = SimpleNamespace(
        output=tmp_path / "output",
        fixture=tmp_path / "fixture",
        deadline=time.time() + 600,
    )
    with pytest.raises(AssertionError, match="retained comparisons"):
        asyncio.run(scenario.run_cache_diagnostic(args))
    proof = json.loads((args.output / "gpu-proof.json").read_text())
    assert proof["diagnostic_complete"] is True and proof["status"] == "failed"
    assert set(proof["comparisons"]) == {
        "warm-1-vs-cold",
        "warm-2-vs-cold",
        "flushed-vs-cold",
        "fresh-process-vs-cold",
    }
    assert proof["comparisons"]["warm-1-vs-cold"]["passed"] is False
    assert proof["comparisons"]["flushed-vs-cold"]["passed"] is True
    assert proof["comparisons"]["fresh-process-vs-cold"]["passed"] is True
    assert sum(event[0] == "start" for event in events) == 2
    assert sum(event[0] == "flush" for event in events) == 1
    assert (args.output / "diagnostics.tar.gz").exists()


def test_health_access_logs_do_not_count_as_native_progress():
    text = "[time] Load weight end\n[time] INFO GET /health HTTP/1.1 503\n[time] INFO GET /chalk/health HTTP/1.1 503\n"
    assert scenario.meaningful_log_lines(text) == ["[time] Load weight end"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("enable_deterministic_inference", False),
        ("attention_backend", "flashinfer"),
        ("sampling_backend", "flashinfer"),
        ("disable_radix_cache", True),
        ("disable_cuda_graph", False),
        ("weight_cache_mode", "auto"),
        ("enable_deterministic_inference", 1),
    ],
)
def test_diagnostic_requires_exact_actual_resolved_config(field, value):
    config = dict(
        enable_deterministic_inference=True,
        attention_backend="triton",
        sampling_backend="pytorch",
        disable_radix_cache=False,
        disable_cuda_graph=True,
        weight_cache_mode="off",
    )
    scenario.require_deterministic_config(
        {"config": config, "internal_states": [dict(config)]}
    )
    bad = {**config, field: value}
    with pytest.raises(RuntimeError, match="contract differs"):
        scenario.require_deterministic_config({"config": bad})
    with pytest.raises(RuntimeError, match="configs disagree"):
        scenario.require_deterministic_config(
            {"config": config, "internal_states": [bad]}
        )
    with pytest.raises(RuntimeError):
        scenario.require_deterministic_config({"config": {}})


def test_diagnostic_overlay_delta_is_only_experimental_adapter():
    original = {gpu.DIAGNOSTIC_ADAPTER: "old", "other.py": "unchanged"}
    current = {**original, gpu.DIAGNOSTIC_ADAPTER: "new"}
    gpu.check_overlay(current, original, True)
    with pytest.raises(ValueError):
        gpu.check_overlay(current, original, False)
    with pytest.raises(ValueError):
        gpu.check_overlay({**current, "other.py": "changed"}, original, True)
    with pytest.raises(ValueError):
        gpu.check_overlay({**current, "extra.py": "new"}, original, True)


def test_diagnostic_plan_binds_actual_attestation_and_only_adapter_delta(
    prepared, monkeypatch
):
    out, _ = prepared
    root = out.parent
    adapter = root / "sdk" / gpu.DIAGNOSTIC_ADAPTER
    adapter.parent.mkdir(parents=True)
    adapter.write_text("# previous adapter\n")
    old = gpu.base.overlay_archive(root / "sdk")[1]
    cpu_plan = root / "cpu" / "plan.json"
    gpu.base.write_json(cpu_plan, {"overlay_files": old})
    ledger = gpu.base.read_json(root / "cpu" / "launcher.json")
    ledger["plan_sha256"] = gpu.payload.digest(cpu_plan)
    gpu.base.write_json(root / "cpu" / "launcher.json", ledger)
    adapter.write_text("# diagnostic adapter\n")
    attestation = root / "attestation.json"
    gpu.base.write_json(
        attestation,
        {
            "passed": True,
            "result": {
                "sources": [
                    {
                        "path": f"module-{index}.py",
                        "sha256": "f" * 64,
                        "bytes": 1,
                        "match": True,
                    }
                    for index in range(13)
                ]
            },
        },
    )
    monkeypatch.setattr(
        gpu, "DETERMINISTIC_ATTESTATION_SHA256", gpu.payload.digest(attestation)
    )
    capacity = root / "profile_capacity.py"
    capacity.write_text("# unchanged shared helper fixture\n")
    monkeypatch.setattr(gpu, "CAPACITY_SOURCE_SHA256", gpu.payload.digest(capacity))
    target = root / "deterministic-plan"
    plan = gpu.prepare(
        target,
        root / "sdk",
        root / "guards",
        root / "upstream",
        root / "cpu",
        deterministic_triton=True,
        source_attestation=attestation,
        capacity_source=capacity,
    )
    assert gpu.validate(target) == plan
    assert plan["lease_seconds"] == 900 and plan["cleanup_reserve_seconds"] == 120
    assert plan["scenario"] == "deterministic_triton_full_diagnostic"
    assert plan["tolerance"] == 1e-3 and len(plan["deterministic_source_hashes"]) == 13
    assert set(plan["diagnostic_overlay_delta"]) == {gpu.DIAGNOSTIC_ADAPTER}
    (target / "deterministic-source-attestation.json").write_text("changed")
    with pytest.raises(ValueError, match="exact independently reviewed"):
        gpu.validate(target)


def test_full_diagnostic_cache_cases_continue_after_numerical_failure(
    tmp_path, monkeypatch
):
    import asyncio

    calls, comparisons = [], {}

    class Response:
        status = 200

        async def text(self):
            return "flushed"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Client:
        def post(self, url, **kwargs):
            calls.append("flush")
            return Response()

    class Native:
        async def start(self, *args):
            calls.append("fresh-start")

        async def stop(self):
            calls.append("stop")

    values = iter([0.02, 0.03, 0.0, 0.0])

    async def samples(*args, **kwargs):
        calls.append(kwargs["label"])
        return sample(next(values))

    def evaluate(observed, expected, *, out, label):
        try:
            scenario.compare(observed, expected, out=out, label=label)
            comparisons[label] = True
        except scenario.NativeComparisonError:
            comparisons[label] = False

    monkeypatch.setattr(scenario, "samples", samples)
    asyncio.run(
        scenario.same_weight_cache(
            Client(), Native(), SimpleNamespace(output=tmp_path), sample(), evaluate
        )
    )
    assert comparisons == {
        "warm-1-vs-cold": False,
        "warm-2-vs-cold": False,
        "flushed-vs-cold": True,
        "fresh-process-vs-cold": True,
    }
    assert calls == [
        "warm-v0-1",
        "warm-v0-2",
        "flush",
        "flushed-cold-v0",
        "stop",
        "fresh-start",
        "fresh-process-v0",
    ]


def test_full_diagnostic_distinguishes_reached_failure_from_unreached(
    tmp_path, monkeypatch
):
    import asyncio

    class Native:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self, *args, **kwargs):
            raise RuntimeError("native profile resolution failed")

        async def stop(self):
            pass

    fixture = tmp_path / "fixture"
    (fixture / "stream").mkdir(parents=True)
    args = SimpleNamespace(
        output=tmp_path / "output",
        fixture=fixture,
        deadline=time.time() + 900,
        deterministic_triton=True,
    )
    monkeypatch.setattr(scenario, "Native", Native)
    with pytest.raises(RuntimeError, match="profile resolution"):
        asyncio.run(scenario.run(args))
    proof = json.loads((args.output / "gpu-proof.json").read_bytes())
    assert proof["status"] == "failed"
    assert proof["scenarios"]["cold_v0"]["status"] == "failed"
    assert proof["scenarios"]["same_weight_cache"]["status"] == "not_reached"
    assert proof["scenarios"]["fresh_native_oracle_v2"]["status"] == "not_reached"
    assert proof["native_groups_stopped"] is True


def native_capacity():
    return {
        "memory": {"MemAvailable": 32 * 1024**3},
        "cpu_affinity_count": 8,
        "cgroup_v2": [
            {
                "memory.max": str(40 * 1024**3),
                "memory.current": str(8 * 1024**3),
                "cpu.max": "800000 100000",
            }
        ],
        "filesystems": {
            name: {"free_bytes": 8 * 1024**3} for name in ("/var/lib/docker", "/tmp")
        },
        "hypervisor_paths": {
            name: {"free_bytes": 8 * 1024**3}
            for name in ("/tmp", "/var/lib/chalk-hypervisor")
        },
    }


def test_native_capacity_separate_small_gate_rejects_each_insufficient_resource():
    import copy

    original = native_capacity()
    assert gpu.validate_small_capacity(original) == original
    cases = [
        lambda value: value["memory"].update(MemAvailable=32 * 1024**3 - 1),
        lambda value: value.update(cpu_affinity_count=7),
        lambda value: value["cgroup_v2"][0].update(
            **{"memory.current": str(8 * 1024**3 + 1)}
        ),
        lambda value: value["cgroup_v2"][0].update(**{"cpu.max": "799999 100000"}),
        lambda value: value["filesystems"]["/var/lib/docker"].update(
            free_bytes=8 * 1024**3 - 1
        ),
        lambda value: value["hypervisor_paths"]["/tmp"].update(
            free_bytes=8 * 1024**3 - 1
        ),
        lambda value: value.update(cgroup_v2=[]),
    ]
    for mutate in cases:
        value = copy.deepcopy(original)
        mutate(value)
        with pytest.raises(RuntimeError):
            gpu.validate_small_capacity(value)


def test_execute_clock_starts_before_validation(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(gpu.time, "time", lambda: events.append("clock") or 1000)

    def validate(out):
        events.append("validate")
        raise RuntimeError("stop before any auth or claim")

    monkeypatch.setattr(gpu, "validate", validate)
    with pytest.raises(RuntimeError, match="before any auth"):
        gpu.execute(tmp_path, None)
    assert events == ["clock", "validate"]


def test_sampled_owned_disk_gate_retains_limitation_and_rejects_large_or_symlink(
    tmp_path,
):
    file = tmp_path / "ordinary"
    file.write_bytes(b"123")
    assert scenario.check_owned_disk(tmp_path) == 3
    proof = json.loads((tmp_path / "native-disk-usage.json").read_bytes())
    assert proof["hard_quota"] is False
    file.write_bytes(b"")
    with file.open("wb") as handle:
        handle.truncate(6 * 1024**3 + 1)
    with pytest.raises(RuntimeError, match="exceed6GiB"):
        scenario.check_owned_disk(tmp_path)
    file.unlink()
    file.symlink_to("/tmp")
    with pytest.raises(RuntimeError, match="inventory"):
        scenario.check_owned_disk(tmp_path)
