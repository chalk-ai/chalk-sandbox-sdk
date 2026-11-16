from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from chalkcompute._gen.chalk.modeldeployment.v1 import service_pb as pb
from chalkcompute._gen.chalk.modeldeployment.v1 import weight_runtime_pb as runtime_pb
from protobuf.wkt import Timestamp
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from chalkcompute._gen.chalk.volume.v2.volume_pb import VolumeRef
from chalkcompute._gen.chalk.weightstream.v1.service_pb import WeightStreamVersion
from chalkcompute.weights.serving import model_target as delivery
from chalkcompute.weights.serving.managed_target import (
    ManagedTargetError,
    ManagedTargetSuperseded,
    ModelTargetNotSelected,
)


@dataclass
class Clock:
    wall: float
    steady: float = 100.0

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.steady

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.steady += seconds


def response(
    generation: int = 1, version: int = 0
) -> runtime_pb.GetModelWeightRuntimeTargetResponse:
    expiry = delivery.time.time() + 15
    return runtime_pb.GetModelWeightRuntimeTargetResponse(
        target=pb.ModelScalingGroupWeightTarget(
            model_scaling_group_id="deployment",
            model_scaling_group_revision_id="revision",
            generation=generation,
            volume=VolumeRef(volume_id="volume"),
            version=WeightStreamVersion(
                weight_stream_id="stream",
                version=version,
                volume_version_id=42,
                manifest_sha256="a" * 64,
                weight_schema_sha256="b" * 64,
            ),
        ),
        lease_expires_at=Timestamp(seconds=int(expiry), nanos=int((expiry % 1) * 1e9)),
    )


def jwt(path: Path, *, env: str = "environment", expiry: float | None) -> str:
    claims: dict[str, object] = {"environment_id": env}
    if expiry is not None:
        claims["exp"] = expiry
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    value = f"test.{payload}.signature"
    path.write_text(value)
    return value


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock = Clock(time.time())
    monkeypatch.setattr(delivery, "time", clock)
    token_file = tmp_path / "identity"
    token = jwt(token_file, expiry=clock.wall + 60)
    rpc = Mock(return_value=response())
    client = Mock(return_value=SimpleNamespace(get_model_weight_runtime_target=rpc))
    monkeypatch.setattr(
        delivery.weight_runtime_connect, "ModelWeightRuntimeServiceClientSync", client
    )
    provider = delivery.ModelDeploymentTargetProvider(
        api_server="https://api.example.test",
        token_file=token_file,
        expected_environment_id="environment",
        expected_deployment_id="deployment",
        expected_revision_id="revision",
        expected_stream_id="stream",
        expected_volume_id="volume",
    )
    return SimpleNamespace(
        provider=provider,
        clock=clock,
        token_file=token_file,
        token=token,
        rpc=rpc,
        client=client,
    )


def test_read_zero_and_cache_exact_identity(setup):
    target = setup.provider.current()
    assert target.logical_version == 0
    assert target.target_generation == 1
    assert target.volume_version_id == 42
    assert target.lease_expires_at.timestamp() == pytest.approx(setup.clock.wall + 15)
    assert setup.provider.current() == target
    assert setup.rpc.call_count == 1
    request = setup.rpc.call_args.args[0]
    assert request.expected_revision_id == "revision"
    assert setup.rpc.call_args.kwargs["timeout_ms"] == 3000
    assert setup.rpc.call_args.kwargs["headers"] == {
        "authorization": f"Bearer {setup.token}",
        "x-chalk-env-id": "environment",
        "x-chalk-server": "go-api",
    }


def test_refresh_renews_and_rotates_explicit_token(setup):
    first = setup.provider.current()
    setup.clock.advance(3)
    token = jwt(setup.token_file, expiry=setup.clock.wall + 100)
    setup.rpc.return_value = response()
    second = setup.provider.assert_current(first)
    assert second.lease_expires_at > first.lease_expires_at
    assert setup.rpc.call_count == 2
    assert setup.rpc.call_args.kwargs["headers"]["authorization"] == f"Bearer {token}"


def test_rpc_latency_consumes_lease(setup):
    def slow(*args, **kwargs):
        setup.clock.advance(4)
        return response()

    setup.rpc.side_effect = slow
    target = setup.provider.current()
    assert target.lease_expires_at.timestamp() - setup.clock.wall == pytest.approx(11)


def test_late_response_rejected_even_when_wall_clock_rolls_back(setup):
    def slow(*args, **kwargs):
        setup.clock.steady += 16
        setup.clock.wall -= 100
        return response()

    setup.rpc.side_effect = slow
    with pytest.raises(ManagedTargetError, match="exhausted"):
        setup.provider.current()


def test_failed_refresh_does_not_reuse_cached_target_or_print_upstream(setup):
    setup.provider.current()
    setup.clock.advance(3)
    setup.rpc.side_effect = RuntimeError("sensitive request header")
    for _ in range(2):
        with pytest.raises(ManagedTargetError, match="refresh failed") as exc:
            setup.provider.current()
        assert "sensitive" not in str(exc.value)
        assert exc.value.__cause__ is None
    assert setup.rpc.call_count == 3


@pytest.mark.parametrize(
    "field", ["missing", "environment", "expired", "no-expiry", "malformed"]
)
def test_bad_workload_identity_fails_before_rpc(setup, field):
    if field == "missing":
        setup.token_file.unlink()
    elif field == "malformed":
        setup.token_file.write_text("not-a-token")
    else:
        jwt(
            setup.token_file,
            env="other" if field == "environment" else "environment",
            expiry=None
            if field == "no-expiry"
            else setup.clock.wall - 1
            if field == "expired"
            else setup.clock.wall + 60,
        )
    with pytest.raises(ManagedTargetError):
        setup.provider.current()
    assert setup.rpc.call_count == 0


def test_token_expiration_caps_target_lease(setup):
    jwt(setup.token_file, expiry=setup.clock.wall + 5)
    target = setup.provider.current()
    assert target.lease_expires_at.timestamp() == pytest.approx(setup.clock.wall + 5)


@pytest.mark.parametrize(
    "field",
    [
        "target",
        "target-owner",
        "target-revision",
        "stream",
        "volume",
        "snapshot",
        "digest",
        "lease",
    ],
)
def test_mismatched_or_missing_identity_fails_closed(setup, field):
    value = response()
    target = value.target
    assert (
        target is not None and target.version is not None and target.volume is not None
    )
    if field == "target":
        value.target = None
    elif field == "target-owner":
        target.model_scaling_group_id = "other"
    elif field == "target-revision":
        target.model_scaling_group_revision_id = "other"
    elif field == "stream":
        target.version.weight_stream_id = "other"
    elif field == "volume":
        target.volume.volume_id = "other"
    elif field == "snapshot":
        target.version.volume_version_id = 0
    elif field == "digest":
        target.version.manifest_sha256 = "bad"
    elif field == "lease":
        value.lease_expires_at = None
    setup.rpc.return_value = value
    with pytest.raises(ManagedTargetError):
        setup.provider.current()


def test_only_explicit_unselected_response_is_retryable(setup):
    setup.rpc.side_effect = ConnectError(
        Code.UNAVAILABLE, "deployment weight target has not been selected"
    )
    with pytest.raises(ModelTargetNotSelected):
        setup.provider.current()
    setup.rpc.side_effect = ConnectError(Code.UNAVAILABLE, "backend unavailable")
    with pytest.raises(ManagedTargetError) as error:
        setup.provider.current()
    assert not isinstance(error.value, ModelTargetNotSelected)


def test_server_lease_caps_observation(setup):
    value = response()
    value.lease_expires_at = Timestamp(seconds=int(setup.clock.wall + 4))
    setup.rpc.return_value = value
    target = setup.provider.current()
    assert 3 < target.lease_expires_at.timestamp() - setup.clock.wall <= 4


def test_generation_and_publication_fences_survive_refresh(setup):
    first = setup.provider.current()
    setup.clock.advance(3)
    setup.rpc.return_value = response(2, 2)
    with pytest.raises(ManagedTargetError, match="changed"):
        setup.provider.assert_current(first)
    setup.clock.advance(3)
    setup.rpc.return_value = response(1, 0)
    with pytest.raises(ManagedTargetError, match="backwards"):
        setup.provider.current()
    setup.rpc.return_value = response(2, 3)
    with pytest.raises(ManagedTargetError, match="drifted"):
        setup.provider.current()


@pytest.mark.parametrize(
    "origin",
    [
        "http://api.test",
        "https://user:pass@api.test",
        "https://api.test/path",
        "https://api.test?query",
        "https://api.test#fragment",
        "https://",
    ],
)
def test_https_origin_required(setup, origin):
    with pytest.raises(ValueError, match="HTTPS"):
        delivery.ModelDeploymentTargetProvider(
            api_server=origin,
            token_file=setup.token_file,
            expected_environment_id="environment",
            expected_deployment_id="deployment",
            expected_revision_id="revision",
            expected_stream_id="stream",
            expected_volume_id="volume",
        )


def test_cli_explicit_source_and_no_ambient_fallback(setup, monkeypatch):
    from chalkcompute.weights.serving import __main__ as cli

    monkeypatch.delenv("CHALK_WEIGHT_TARGET_FILE", raising=False)
    monkeypatch.setenv("CHALK_WEIGHT_TARGET_SOURCE", "model-deployment")
    bindings = {
        "CHALK_MODEL_SCALING_GROUP_ID": "deployment",
        "CHALK_MODEL_SCALING_GROUP_REVISION_ID": "revision",
        "CHALK_WEIGHT_STREAM_ID": "stream",
        "CHALK_WEIGHT_VOLUME_ID": "volume",
        "CHALK_ENVIRONMENT_ID": "environment",
        "CHALK_FUNCTION_QUEUE_TOKEN_FILE": str(setup.token_file),
    }
    for name, value in bindings.items():
        monkeypatch.setenv(name, value)
    assert cli._managed_target_provider(SimpleNamespace()) is not None
    monkeypatch.delenv("CHALK_FUNCTION_QUEUE_TOKEN_FILE")
    monkeypatch.setenv("CHALK_CLIENT_ID", "ambient-client")
    monkeypatch.setenv("CHALK_CLIENT_SECRET", "ambient-secret")
    with pytest.raises(ValueError, match="CHALK_FUNCTION_QUEUE_TOKEN_FILE"):
        cli._managed_target_provider(SimpleNamespace())


def test_cli_rejects_mixed_and_unknown_sources(monkeypatch):
    from chalkcompute.weights.serving import __main__ as cli

    monkeypatch.setenv("CHALK_WEIGHT_TARGET_SOURCE", "model-deployment")
    monkeypatch.setenv("CHALK_WEIGHT_TARGET_FILE", "target.json")
    with pytest.raises(ValueError, match="cannot be combined"):
        cli._managed_target_provider(SimpleNamespace())
    monkeypatch.setenv("CHALK_WEIGHT_TARGET_SOURCE", "unknown")
    with pytest.raises(ValueError, match="Unsupported"):
        cli._managed_target_provider(SimpleNamespace())


def test_response_later_than_token_expiry_is_refused(setup):
    jwt(setup.token_file, expiry=setup.clock.wall + 1)

    def slow(*args, **kwargs):
        setup.clock.advance(2)
        return response()

    setup.rpc.side_effect = slow
    with pytest.raises(ManagedTargetError, match="exhausted"):
        setup.provider.current()


def test_short_server_lease_refreshes_before_poll_interval(setup):
    value = response()
    expiry = setup.clock.wall + 0.5
    value.lease_expires_at = Timestamp(
        seconds=int(expiry), nanos=int((expiry % 1) * 1e9)
    )
    setup.rpc.return_value = value
    first = setup.provider.current()
    setup.clock.advance(0.6)
    setup.rpc.return_value = response()
    second = setup.provider.current()
    assert setup.rpc.call_count == 2
    assert second.lease_expires_at > first.lease_expires_at


def _manifest_response(versions=(0,), *, generation=1):
    result = response(generation)
    result.manifest_identities.extend(
        runtime_pb.ModelWeightRuntimeManifestIdentity(
            version=version, manifest_sha256="a" * 64 if version == 0 else "b" * 64
        )
        for version in versions
    )
    return result


def test_manifest_identity_refresh_keeps_cached_lease_consistent(setup):
    target = setup.provider.current()
    setup.clock.advance(1)
    setup.rpc.return_value = _manifest_response()
    assert setup.provider.manifest_digests(target, (0,)) == {0: "a" * 64}
    request = setup.rpc.call_args.args[0]
    assert request.manifest_versions == [0]
    renewed = setup.provider.current()
    assert renewed.lease_expires_at > target.lease_expires_at
    assert setup.provider.assert_current(target) == renewed
    assert setup.rpc.call_count == 2


@pytest.mark.parametrize(
    "malformation", ["missing", "duplicate", "unrequested", "digest", "leaf", "target"]
)
def test_manifest_identity_response_fails_closed(setup, malformation):
    target = setup.provider.current()
    result = _manifest_response()
    if malformation == "missing":
        result.manifest_identities.clear()
    elif malformation == "duplicate":
        result.manifest_identities.append(result.manifest_identities[0])
    elif malformation == "unrequested":
        result.manifest_identities.append(
            runtime_pb.ModelWeightRuntimeManifestIdentity(
                version=1, manifest_sha256="a" * 64
            )
        )
    elif malformation == "digest":
        result.manifest_identities[0].manifest_sha256 = "z" * 64
    elif malformation == "leaf":
        result.manifest_identities[0].manifest_sha256 = "b" * 64
    else:
        result.target.version.version += 1
    setup.rpc.return_value = result
    with pytest.raises(ManagedTargetError):
        setup.provider.manifest_digests(target, (0,))
    assert setup.provider._target is None


@pytest.mark.parametrize(
    "versions", [(), (0, 0), (-1, 0), (True,), (1,), tuple(range(129))]
)
def test_manifest_identity_request_is_bounded(setup, versions):
    target = setup.provider.current()
    before = setup.rpc.call_count
    with pytest.raises(ManagedTargetError, match="bounded"):
        setup.provider.manifest_digests(target, versions)
    assert setup.rpc.call_count == before


def test_manifest_identity_refresh_serializes_with_lease_watcher(setup):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    target = setup.provider.current()
    setup.clock.advance(1)
    entered = threading.Event()
    release = threading.Event()

    def fetch(request, **kwargs):
        assert request.manifest_versions == [0]
        entered.set()
        assert release.wait(2)
        return _manifest_response()

    setup.rpc.side_effect = fetch
    with ThreadPoolExecutor(max_workers=2) as executor:
        digest = executor.submit(setup.provider.manifest_digests, target, (0,))
        assert entered.wait(2)
        watcher = executor.submit(setup.provider.current)
        release.set()
        assert digest.result(timeout=2) == {0: "a" * 64}
        renewed = watcher.result(timeout=2)
        assert renewed.lease_expires_at > target.lease_expires_at
        assert setup.provider.assert_current(target) == renewed


def test_same_publication_newer_generation_defers_only_after_verified_identity(setup):
    target = setup.provider.current()
    setup.clock.advance(1)
    setup.rpc.return_value = _manifest_response(generation=2)
    with pytest.raises(ManagedTargetSuperseded):
        setup.provider.manifest_digests(target, (0,))
    latest = setup.provider.current()
    assert latest.target_generation == 2
    assert latest.fingerprint() != target.fingerprint()
    assert setup.rpc.call_count == 2
    assert setup.provider.manifest_digests(latest, (0,)) == {0: "a" * 64}


@pytest.mark.parametrize(
    "invalid",
    [
        "owner",
        "revision",
        "stream",
        "volume",
        "snapshot",
        "manifest",
        "schema",
        "logical-version",
        "missing-identity",
        "duplicate-identity",
        "leaf-identity",
        "expired-lease",
        "permission",
        "generation-rollback",
        "equal-generation-drift",
    ],
)
def test_invalid_generation_refresh_never_becomes_retryable(setup, invalid):
    target = setup.provider.current()
    result = _manifest_response(generation=2)
    selected = result.target
    version = selected.version
    if invalid == "owner":
        selected.model_scaling_group_id = "other"
    elif invalid == "revision":
        selected.model_scaling_group_revision_id = "other"
    elif invalid == "stream":
        version.weight_stream_id = "other"
    elif invalid == "volume":
        selected.volume.volume_id = "other"
    elif invalid == "snapshot":
        version.volume_version_id += 1
    elif invalid == "manifest":
        version.manifest_sha256 = "c" * 64
    elif invalid == "schema":
        version.weight_schema_sha256 = "c" * 64
    elif invalid == "logical-version":
        version.version += 1
    elif invalid == "missing-identity":
        result.manifest_identities.clear()
    elif invalid == "duplicate-identity":
        result.manifest_identities.append(result.manifest_identities[0])
    elif invalid == "leaf-identity":
        result.manifest_identities[0].manifest_sha256 = "c" * 64
    elif invalid == "expired-lease":
        result.lease_expires_at = Timestamp(seconds=int(setup.clock.wall - 1))
    elif invalid == "permission":
        setup.rpc.side_effect = ConnectError(Code.PERMISSION_DENIED, "not authorized")
    elif invalid == "generation-rollback":
        setup.rpc.return_value = response(generation=3)
        setup.clock.advance(3)
        setup.provider.current()
    else:
        selected.generation = target.target_generation
        version.volume_version_id += 1
    setup.rpc.return_value = result
    with pytest.raises(ManagedTargetError) as error:
        setup.provider.manifest_digests(target, (0,))
    assert not isinstance(error.value, ManagedTargetSuperseded)
    assert setup.provider._target is None


def test_runtime_replans_same_publication_race_without_weight_writes(
    setup, tmp_path, monkeypatch
):
    import asyncio
    import hashlib
    from unittest.mock import AsyncMock

    from chalkcompute._weights_codec import checksum
    from chalkcompute._weights_format import TensorEntry, WeightManifest, version_dir
    from chalkcompute.weights.layout import WeightSchema
    from chalkcompute.weights.serving import model_runtime
    from chalkcompute.weights.serving.adapters import vllm_resident
    from chalkcompute.weights.serving.sparse_syncer import SparseWeightSyncer

    root = tmp_path / "weights"
    seed = root / version_dir(0)
    seed.mkdir(parents=True)
    payload = b"\x00\x00"
    (seed / "weight").write_bytes(payload)
    manifest = WeightManifest(
        0,
        None,
        "keyframe",
        "test-model",
        "a" * 64,
        (
            TensorEntry(
                "weight",
                "bfloat16",
                (1,),
                "dense",
                "weight",
                0,
                2,
                1,
                checksum(payload),
            ),
        ),
    )
    (seed / "manifest.json").write_bytes(manifest.to_json())
    digest = hashlib.sha256(manifest.to_json()).hexdigest()
    schema = WeightSchema.from_manifest(manifest).sha256
    requests = 0
    generation = 1

    def fetch(request, **kwargs):
        nonlocal requests, generation
        if request.manifest_versions:
            requests += 1
            if requests == 2:
                generation = 2
        result = _manifest_response(generation=generation)
        result.target.version.manifest_sha256 = digest
        result.target.version.weight_schema_sha256 = schema
        result.manifest_identities[0].manifest_sha256 = digest
        return result

    setup.rpc.side_effect = fetch

    async def run():
        completed = asyncio.Event()
        process_done = asyncio.Event()
        paused_before_retry = []
        fingerprints = []

        class Handle:
            requires_target = True

            def set_serving(self, serving, **kwargs):
                if serving:
                    fingerprints.append(kwargs["target_fingerprint"])

            def wait_drained(self, timeout_seconds):
                return True

        process = SimpleNamespace(pid=123456789, returncode=None)

        async def wait():
            await process_done.wait()
            return process.returncode

        process.wait = wait

        def killpg(pid, sig):
            assert pid == process.pid
            process.returncode = 0
            process_done.set()

        class Adapter:
            def __init__(self, *args, **kwargs):
                pass

            def launch_cmd(self, directory, port):
                return ["not-executed"]

            async def ready(self):
                return True

            async def initialize(self, *args, **kwargs):
                pass

            async def apply(self, *args, **kwargs):
                raise AssertionError("Unexpected weight write")

            async def reset_cache(self):
                raise AssertionError("Unexpected cache reset")

        def make_syncer(root, adapter, gate, **kwargs):
            syncer = SparseWeightSyncer(
                root, adapter, gate, poll_seconds=0.01, reload=lambda _: None, **kwargs
            )
            original = syncer.sync_once

            async def sync_once():
                result = await original()
                assert result is False
                if gate.paused:
                    paused_before_retry.append(gate.applied_target["target_generation"])
                    assert gate.healthy and not gate.failed.is_set() and not gate.ready
                elif gate.applied_target["target_generation"] == 2:
                    assert paused_before_retry == [1]
                    assert gate.ready and gate.healthy and not gate.failed.is_set()
                    completed.set()
                return result

            syncer.sync_once = sync_once
            return syncer

        monkeypatch.setattr(vllm_resident, "VLLMResidentAdapter", Adapter)
        monkeypatch.setattr(model_runtime, "SparseWeightSyncer", make_syncer)
        monkeypatch.setattr(model_runtime, "os", SimpleNamespace(killpg=killpg))
        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
        )
        runtime = model_runtime.ManagedModelRuntime(
            model_runtime.ModelServingConfig(
                "vllm-sparse", [], root, tmp_path / "layout.json", "a" * 64, 8001, 2
            ),
            Handle(),
            setup.provider,
        )
        task = asyncio.create_task(runtime._run())
        try:
            await asyncio.wait_for(completed.wait(), 2)
            assert process.returncode is None and not task.done()
            assert len(set(fingerprints)) == 2
            assert runtime.gate.applied_target["target_generation"] == 2
        finally:
            runtime._stop.set()
            await asyncio.wait_for(task, 2)

    asyncio.run(run())
