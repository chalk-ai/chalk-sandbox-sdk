"""Public adapter/control policy tests; native CUDA admission is mocked."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from chalkcompute._weights_codec import TensorPatch, checksum, encode_patch
from chalkcompute._weights_format import (
    TensorEntry,
    WeightManifest,
    WeightStreamError,
    pointer_json,
    version_dir,
)
from chalkcompute.weights.layout import WeightLayout, WeightSchema, TensorSpec
from chalkcompute.weights.serving import resident as core
from chalkcompute.weights.serving import (
    resident_runtime,
    vllm_resident_server,
    vllm_resident_worker,
)
from chalkcompute.weights.serving.adapters import vllm_resident
from chalkcompute.weights.serving.sparse_syncer import (
    RestartRequired,
    SparseWeightSyncer,
)
from chalkcompute.weights.serving.supervisor import AdmissionGate
from chalkcompute.weights.serving.syncer import ApplyError, StreamError, WeightSyncer

torch = pytest.importorskip("torch")


@pytest.fixture(autouse=True)
def clean_vllm_environment(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("VLLM_"):
            monkeypatch.delenv(name)


_LAYOUT_RAW = b""
_LAYOUT_PATH = None
_LAYOUT = None


@pytest.fixture(autouse=True)
def pinned_layout_file(tmp_path):
    global _LAYOUT_RAW, _LAYOUT_PATH, _LAYOUT
    _LAYOUT = WeightLayout().bind(
        WeightSchema((TensorSpec("model.norm.weight", (4,), "bfloat16"),))
    )
    _LAYOUT_RAW = _LAYOUT.to_json()
    _LAYOUT_PATH = tmp_path / "explicit-weight-layout.json"
    _LAYOUT_PATH.write_bytes(_LAYOUT_RAW)


def _adapter(*args, **kwargs):
    return vllm_resident.VLLMResidentAdapter(
        *args,
        weight_layout=_LAYOUT_PATH,
        weight_layout_sha256=hashlib.sha256(_LAYOUT_RAW).hexdigest(),
        **kwargs,
    )


def config():
    return json.dumps({**{"model_type": "tiny"}, "torch_dtype": "bfloat16"}).encode()


def manifest_at(directory, version=0, parent=None, kind="keyframe", tensors=None):
    directory.mkdir(exist_ok=True)
    if tensors is None:
        tensors = (
            TensorEntry(
                "model.norm.weight",
                "bfloat16",
                (4,),
                "dense",
                "model.safetensors",
                0,
                8,
                4,
                "a" * 32,
            ),
        )
    manifest = WeightManifest(version, parent, kind, "example/tiny", "a" * 64, tensors)
    raw = manifest.to_json()
    (directory / "manifest.json").write_bytes(raw)
    (directory / "config.json").write_bytes(config())
    return manifest, hashlib.sha256(raw).hexdigest()


def ack(manifest, digest, identity="b" * 64):
    return {
        "ok": True,
        "version": manifest.version,
        "manifest_sha256": digest,
        "identity_sha256": identity,
        "profile": core.PROFILE,
        "layout_sha256": _LAYOUT.sha256,
        "schema_sha256": _LAYOUT.source_schema_sha256,
    }


def worker_environment(monkeypatch):
    for key, value in {
        "VLLM_BATCH_INVARIANT": "1",
        "VLLM_SERVER_DEV_MODE": "1",
        "VLLM_WORKER_MULTIPROC_METHOD": "spawn",
    }.items():
        monkeypatch.setenv(key, value)


def worker(tmp_path, monkeypatch):
    worker_environment(monkeypatch)
    monkeypatch.setattr(resident_runtime, "check_cuda", lambda _: None)
    monkeypatch.setattr(resident_runtime, "check_runtime_sources", lambda _: None)
    model = SimpleNamespace(parameters=lambda: iter([SimpleNamespace(device="cuda:0")]))
    native_config = SimpleNamespace(
        parallel_config=SimpleNamespace(
            tensor_parallel_size=1, pipeline_parallel_size=1, data_parallel_size=1
        ),
        model_config=SimpleNamespace(
            dtype=torch.bfloat16,
            quantization=None,
            trust_remote_code=False,
            enforce_eager=True,
            model=str(tmp_path),
        ),
        speculative_config=None,
        cache_config=SimpleNamespace(enable_prefix_caching=False),
    )
    instance = vllm_resident_worker.ResidentSparseExtension()
    instance.vllm_config = native_config
    instance.model_runner = SimpleNamespace(get_model=lambda: model)
    return instance, model


@pytest.mark.parametrize(
    "field,value",
    [
        ("tensor_parallel_size", 2),
        ("pipeline_parallel_size", 2),
        ("data_parallel_size", 2),
        ("dtype", torch.float16),
        ("quantization", "fp8"),
        ("trust_remote_code", True),
        ("enforce_eager", False),
        ("speculative_config", object()),
        ("enable_prefix_caching", True),
    ],
)
def test_worker_rejects_resolved_native_contract_drift(
    tmp_path, monkeypatch, field, value
):
    instance, _ = worker(tmp_path, monkeypatch)
    target = instance.vllm_config
    if field.endswith("parallel_size"):
        target = target.parallel_config
    elif field == "enable_prefix_caching":
        target = target.cache_config
    elif field != "speculative_config":
        target = target.model_config
    setattr(target, field, value)
    with pytest.raises(ValueError, match="Resolved"):
        vllm_resident_worker.check_worker(instance)


def test_worker_bind_failure_does_not_publish_resident_and_apply_checks_model(
    tmp_path, monkeypatch
):
    instance, model = worker(tmp_path, monkeypatch)
    manifest, digest = manifest_at(tmp_path)
    events = []

    class Resident:
        fail = True

        def __init__(self, model, config_bytes, initial, layout):
            self.layout_sha256 = layout.sha256
            self.schema_sha256 = layout.source_schema_sha256
            self.model, self.version, self.identity_sha256 = (
                model,
                initial.version,
                "b" * 64,
            )
            assert config_bytes == config() and initial == manifest

        def verify_initial(self, directory):
            events.append("base_audit")
            if self.fail:
                raise WeightStreamError("base mismatch")
            return {"ok": True, "logical_tensors": 1}

        def apply(self, *args):
            events.append("apply")
            return {"ok": True}

    monkeypatch.setattr(core, "ResidentWeights", Resident)
    with pytest.raises(WeightStreamError, match="base mismatch"):
        instance.chalk_resident_bind(
            str(tmp_path),
            0,
            digest,
            _LAYOUT_RAW.decode(),
            hashlib.sha256(_LAYOUT_RAW).hexdigest(),
        )
    assert getattr(instance, "_chalk_resident", None) is None
    Resident.fail = False
    result = instance.chalk_resident_bind(
        str(tmp_path),
        0,
        digest,
        _LAYOUT_RAW.decode(),
        hashlib.sha256(_LAYOUT_RAW).hexdigest(),
    )
    assert result["identity_sha256"] == "b" * 64 and result["manifest_sha256"] == digest
    assert result["logical_tensors"] == 1
    with pytest.raises(ValueError, match="already bound"):
        instance.chalk_resident_bind(
            str(tmp_path),
            0,
            digest,
            _LAYOUT_RAW.decode(),
            hashlib.sha256(_LAYOUT_RAW).hexdigest(),
        )
    assert instance.chalk_resident_apply(str(tmp_path), 1, "c" * 64) == {"ok": True}
    instance.model_runner.get_model = lambda: SimpleNamespace(
        parameters=model.parameters
    )
    with pytest.raises(RuntimeError, match="model object"):
        instance.chalk_resident_apply(str(tmp_path), 1, "c" * 64)
    assert events == ["base_audit", "base_audit", "apply"]


def test_worker_bind_checks_path_and_bounded_config(tmp_path, monkeypatch):
    instance, _ = worker(tmp_path, monkeypatch)
    _, digest = manifest_at(tmp_path)
    instance.vllm_config.model_config.model = str(tmp_path / "other")
    with pytest.raises(ValueError, match="directory"):
        instance.chalk_resident_bind(
            str(tmp_path),
            0,
            digest,
            _LAYOUT_RAW.decode(),
            hashlib.sha256(_LAYOUT_RAW).hexdigest(),
        )
    instance.vllm_config.model_config.model = str(tmp_path)
    (tmp_path / "config.json").write_bytes(b" " * (core.MAX_MANIFEST_BYTES + 1))
    with pytest.raises(WeightStreamError, match="size bound"):
        instance.chalk_resident_bind(
            str(tmp_path),
            0,
            digest,
            _LAYOUT_RAW.decode(),
            hashlib.sha256(_LAYOUT_RAW).hexdigest(),
        )
    assert getattr(instance, "_chalk_resident", None) is None


@pytest.mark.parametrize(
    "option",
    [
        ["--tensor-parallel-size=2"],
        ["--host=0.0.0.0"],
        ["--config=other.yaml"],
        ["--worker-extension-cls=other.Worker"],
        ["--max-model-len=4097"],
        ["--gpu-memory-utilization=nan"],
        ["--gpu-memory-utilization=1"],
        ["--max-model-len"],
        ["--max-model-len=256", "--max-model-len=512"],
    ],
)
def test_launch_refuses_unsupported_or_ambiguous_options(option):
    with pytest.raises(ValueError):
        _adapter(engine_args=option)


def test_launch_pins_network_topology_sparse_worker_and_custom_limits(
    tmp_path, monkeypatch
):
    (tmp_path / "config.json").write_bytes(config())
    monkeypatch.setattr(vllm_resident, "check_runtime_sources", lambda _: None)
    adapter = _adapter(
        engine_args=["--gpu-memory-utilization=0.6", "--max-model-len", "512"]
    )
    command = adapter.launch_cmd(tmp_path, 8001)
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--tensor-parallel-size") + 1] == "1"
    assert command[command.index("--pipeline-parallel-size") + 1] == "1"
    assert "--no-enable-prefix-caching" in command and "--enforce-eager" in command
    assert command[command.index("--worker-extension-cls") + 1].endswith(
        "ResidentSparseExtension"
    )
    assert (
        command.count("--max-model-len") == 1
        and "--gpu-memory-utilization=0.6" in command
    )
    assert "chalkcompute.weights.serving.vllm_resident_server" in command
    with pytest.raises(ValueError, match="port differs"):
        adapter.launch_cmd(tmp_path, 8002)
    (tmp_path / "config.json").write_bytes(b" " * (core.MAX_MANIFEST_BYTES + 1))
    with pytest.raises(WeightStreamError, match="size bound"):
        adapter.launch_cmd(tmp_path, 8001)


def test_native_startup_spawn_and_internal_omp_marker_are_exact(monkeypatch):
    worker_environment(monkeypatch)
    monkeypatch.delenv("VLLM_WORKER_MULTIPROC_METHOD")
    # Pinned commit4ba787e entrypoints/common/startup.py cli_env_setup does
    # exactly this absent-only assignment (source inspected independently).
    if "VLLM_WORKER_MULTIPROC_METHOD" not in os.environ:
        os.environ["VLLM_WORKER_MULTIPROC_METHOD"] = "spawn"
    # Pinned utils/torch_utils.py names this marker; worker startup sets'1'.
    monkeypatch.setenv("VLLM_OMP_NUM_THREADS_SET_BY_VLLM", "1")
    vllm_resident.validate_environment(worker=True)
    for key, value in (
        ("VLLM_WORKER_MULTIPROC_METHOD", "fork"),
        ("VLLM_OMP_NUM_THREADS_SET_BY_VLLM", "0"),
    ):
        with monkeypatch.context() as context:
            context.setenv(key, value)
            with pytest.raises(ValueError):
                vllm_resident.validate_environment(worker=True)
    monkeypatch.delenv("VLLM_BATCH_INVARIANT")
    with pytest.raises(ValueError, match="Required"):
        vllm_resident.validate_environment(worker=True)


def test_server_sets_required_environment_before_native_import(monkeypatch):
    monkeypatch.setattr(resident_runtime, "check_runtime_sources", lambda _: None)
    calls = []

    def run_module(name, run_name):
        calls.append((name, run_name))
        assert (
            os.environ["VLLM_BATCH_INVARIANT"]
            == os.environ["VLLM_SERVER_DEV_MODE"]
            == "1"
        )
        assert os.environ["VLLM_WORKER_MULTIPROC_METHOD"] == "spawn"

    monkeypatch.setattr(vllm_resident_server.runpy, "run_module", run_module)
    vllm_resident_server.main()
    assert calls == [("vllm.entrypoints.openai.api_server", "__main__")]


@pytest.mark.parametrize(
    "wrong",
    [
        "missing",
        "two_workers",
        "bool_version",
        "version",
        "manifest",
        "profile",
        "layout_sha256",
        "schema_sha256",
        "identity",
        "ok",
    ],
)
def test_adapter_refuses_bad_ack_and_fails_sticky(tmp_path, monkeypatch, wrong):
    manifest, digest = manifest_at(tmp_path)
    adapter = _adapter()
    adapter._model_dir = tmp_path.resolve()
    result = ack(manifest, digest)
    if wrong == "bool_version":
        result["version"] = False
    elif wrong in (
        "version",
        "manifest",
        "profile",
        "layout_sha256",
        "schema_sha256",
        "identity",
        "ok",
    ):
        field = {"manifest": "manifest_sha256", "identity": "identity_sha256"}.get(
            wrong, wrong
        )
        result[field] = {
            "version": 9,
            "manifest": "c" * 64,
            "profile": "other",
            "layout_sha256": "c" * 64,
            "schema_sha256": "c" * 64,
            "identity": "bad",
            "ok": False,
        }[wrong]
    response = (
        {"results": [result]}
        if wrong != "two_workers"
        else {"results": [result, result]}
    )
    if wrong == "missing":
        response = {}

    async def post(*args):
        return response

    monkeypatch.setattr(adapter, "_post", post)
    with pytest.raises(RuntimeError):
        asyncio.run(adapter.initialize(tmp_path, manifest))
    assert adapter._failed and adapter._identity is None
    with pytest.raises(RuntimeError, match="restart"):
        asyncio.run(adapter._control("chalk_resident_bind", tmp_path, manifest))


def test_adapter_binds_ack_then_rejects_identity_drift_and_cancellation(
    tmp_path, monkeypatch
):
    manifest, digest = manifest_at(tmp_path)
    adapter = _adapter()
    adapter._model_dir = tmp_path.resolve()
    calls = []

    async def post(path, body):
        calls.append((path, body))
        return {"results": [ack(manifest, digest)]}

    monkeypatch.setattr(adapter, "_post", post)
    asyncio.run(adapter.initialize(tmp_path, manifest))
    assert calls[0][1]["method"] == "chalk_resident_bind"
    assert calls[0][1]["args"] == [
        str(tmp_path.resolve()),
        0,
        digest,
        _LAYOUT_RAW.decode(),
        hashlib.sha256(_LAYOUT_RAW).hexdigest(),
    ]
    patch_dir = tmp_path / "patch"
    patch, patch_digest = manifest_at(patch_dir, 1, 0, "patch")

    async def drift(*args):
        return {"results": [ack(patch, patch_digest, "d" * 64)]}

    monkeypatch.setattr(adapter, "_post", drift)
    with pytest.raises(RuntimeError, match="identity"):
        asyncio.run(adapter.apply(patch_dir, patch))
    assert adapter._failed
    adapter = _adapter()
    adapter._identity = "b" * 64

    async def cancel(*args):
        raise asyncio.CancelledError()

    monkeypatch.setattr(adapter, "_post", cancel)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(adapter.apply(patch_dir, patch))
    assert adapter._failed


def test_adapter_cache_failure_is_sticky(tmp_path, monkeypatch):
    adapter = _adapter()
    adapter._identity = "b" * 64

    async def refuse(*args):
        return {"success": False}

    monkeypatch.setattr(adapter, "_post", refuse)
    with pytest.raises(RuntimeError):
        asyncio.run(adapter.reset_cache())
    assert adapter._failed


def test_control_manifest_is_bounded_before_any_http(tmp_path, monkeypatch):
    manifest, _ = manifest_at(tmp_path, 1, 0, "patch")
    (tmp_path / "manifest.json").write_bytes(b" " * (core.MAX_MANIFEST_BYTES + 1))
    adapter = _adapter()
    adapter._identity = "b" * 64

    async def unexpected(*args):
        pytest.fail("Oversized manifest must precede HTTP control")

    monkeypatch.setattr(adapter, "_post", unexpected)
    with pytest.raises(WeightStreamError, match="size bound"):
        asyncio.run(adapter.apply(tmp_path, manifest))
    assert not adapter._failed


@pytest.mark.parametrize("kind", ["oversized_chunked", "native_error", "http_error"])
def test_sparse_post_bounds_streamed_acks_and_redacts_native_errors(monkeypatch, kind):
    async def scenario():
        reader = asyncio.StreamReader()
        if kind == "oversized_chunked":
            for _ in range(7):
                reader.feed_data(b" " * 10000)
            # Intentionally no EOF: the bound must fail without waiting for it.
        else:
            reader.feed_data(b'{"error":"private-native-diagnostic"}')
            reader.feed_eof()
        exited = []

        class Response:
            status = 500 if kind == "http_error" else 200
            content = reader

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                exited.append("response")

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                exited.append("session")

            def post(self, *args, **kwargs):
                return Response()

        monkeypatch.setattr(vllm_resident, "ClientSession", lambda **kwargs: Session())
        with pytest.raises(RuntimeError) as error:
            await asyncio.wait_for(_adapter()._post("/collective_rpc", {}), 0.5)
        assert "private-native-diagnostic" not in str(error.value)
        assert exited == ["response", "session"]
        if kind == "oversized_chunked":
            assert "exceeds bound" in str(error.value)

    asyncio.run(scenario())


def stream(tmp_path):
    root = tmp_path / "weights"
    root.mkdir()
    manifests = []
    for version in range(3):
        folder = root / version_dir(version)
        folder.mkdir()
        if version:
            values = torch.tensor([version + 1], dtype=torch.bfloat16)
            tensor_patch = TensorPatch(
                "model.norm.weight", (4,), "bfloat16", values, torch.tensor([1])
            )
            payload = encode_patch(tensor_patch)
            filename = "payload.bin"
            (folder / filename).write_bytes(payload)
        else:
            from safetensors.torch import save_file

            filename = "model.safetensors"
            save_file(
                {"model.norm.weight": torch.ones(4, dtype=torch.bfloat16)},
                str(folder / filename),
            )
            payload = (folder / filename).read_bytes()
        entry = TensorEntry(
            "model.norm.weight",
            "bfloat16",
            (4,),
            "sparse" if version else "dense",
            filename,
            0,
            len(payload),
            1 if version else 4,
            checksum(payload),
        )
        manifest, _ = manifest_at(
            folder,
            version,
            version - 1 if version else None,
            "patch" if version else "keyframe",
            (entry,),
        )
        manifests.append(manifest)
    (root / "CURRENT").write_bytes(pointer_json(2))
    return root, manifests


class FakeAdapter:
    def __init__(self, events):
        self.events = events

    async def initialize(self, directory, manifest):
        self.events.append(("initialize", manifest.version))

    async def apply(self, directory, manifest):
        self.events.append(("apply", manifest.version))

    async def reset_cache(self):
        self.events.append(("cache", None))


class LoggingGate(AdmissionGate):
    def __init__(self, events):
        super().__init__()
        self.events = events

    @asynccontextmanager
    async def update(self, timeout):
        async with super().update(timeout):
            self.events.append(("drained", None))
            yield
            self.events.append(("resume", self.version))


def test_sparse_syncer_resolves_skipped_parent_and_orders_preverify_drain_apply_cache(
    tmp_path,
):
    root, _ = stream(tmp_path)

    async def scenario():
        events = []
        gate = LoggingGate(events)
        gate.version = 0
        syncer = SparseWeightSyncer(
            root, FakeAdapter(events), gate, reload=lambda _: None
        )
        assert [m.version for m in syncer.plan(2).manifests] == [1, 2]
        original = syncer.verify

        def verify(plan):
            original(plan)
            events.append(("preverified", tuple(m.version for m in plan.manifests)))

        syncer.verify = verify
        async with gate.admit():
            task = asyncio.create_task(syncer.sync_once())
            for _ in range(50):
                if events:
                    break
                await asyncio.sleep(0.001)
            assert events == [("preverified", (1, 2))]
            assert not task.done()
        assert await task
        assert events == [
            ("preverified", (1, 2)),
            ("drained", None),
            ("apply", 1),
            ("apply", 2),
            ("cache", None),
            ("resume", 2),
        ]
        assert gate.healthy and gate.version == 2

    asyncio.run(scenario())


def test_sparse_preverify_corrupt_final_refuses_without_drain_or_apply(tmp_path):
    root, _ = stream(tmp_path)
    path = root / version_dir(2) / "payload.bin"
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 1
    path.write_bytes(raw)

    async def scenario():
        events = []
        gate = LoggingGate(events)
        gate.version = 0
        syncer = SparseWeightSyncer(
            root, FakeAdapter(events), gate, reload=lambda _: None
        )
        with pytest.raises(WeightStreamError):
            await syncer.sync_once()
        assert not events and gate.healthy and gate.version == 0

    asyncio.run(scenario())


def test_sparse_bootstrap_initializes_after_launch_before_patches_and_version(tmp_path):
    root, _ = stream(tmp_path)

    async def scenario():
        events = []
        gate = AdmissionGate()
        syncer = SparseWeightSyncer(
            root, FakeAdapter(events), gate, reload=lambda _: None
        )

        async def launch(directory):
            assert gate.version is None
            events.append(("launch", directory.name))

        await syncer.bootstrap(launch)
        assert events == [
            ("launch", "v000000"),
            ("initialize", 0),
            ("apply", 1),
            ("apply", 2),
            ("cache", None),
        ]
        assert gate.version == 2 and gate.healthy

    asyncio.run(scenario())


def test_sparse_plan_rejects_dense_and_backward_reload(tmp_path):
    root, manifests = stream(tmp_path)
    gate = AdmissionGate()
    gate.version = 2
    syncer = SparseWeightSyncer(root, FakeAdapter([]), gate, reload=lambda _: None)
    with pytest.raises(RestartRequired):
        syncer.plan(0)
    dense_entry = replace(manifests[2].tensors[0], encoding="dense", nnz=4)
    (root / version_dir(2) / "manifest.json").write_bytes(
        replace(manifests[2], tensors=(dense_entry,)).to_json()
    )
    gate.version = 0
    with pytest.raises(RestartRequired):
        syncer.plan(2)


def test_sparse_cache_failure_fails_admissions_and_preserves_old_policy_version(
    tmp_path,
):
    root, _ = stream(tmp_path)

    async def scenario():
        events = []
        gate = AdmissionGate()
        gate.version = 0
        adapter = FakeAdapter(events)

        async def fail_cache():
            raise RuntimeError("cache reset uncertain")

        adapter.reset_cache = fail_cache
        syncer = SparseWeightSyncer(root, adapter, gate, reload=lambda _: None)
        with pytest.raises(ApplyError):
            await syncer.sync_once()
        assert not gate.healthy and gate.version == 0
        assert events == [("apply", 1), ("apply", 2)]

    asyncio.run(scenario())


def test_sparse_manifest_reader_bounds_and_architecture_binding(tmp_path):
    root, manifests = stream(tmp_path)
    gate = AdmissionGate()
    gate.version = 0
    syncer = SparseWeightSyncer(root, FakeAdapter([]), gate, reload=lambda _: None)
    syncer.architecture_hash = "f" * 64
    with pytest.raises(StreamError, match="architecture"):
        syncer.manifest(1)
    syncer.architecture_hash = manifests[0].architecture_hash
    (root / version_dir(2) / "manifest.json").write_bytes(
        b" " * (core.MAX_MANIFEST_BYTES + 1)
    )
    with pytest.raises(WeightStreamError, match="size bound"):
        syncer.plan(2)
    assert gate.healthy and gate.version == 0


def test_bootstrap_init_failure_never_applies_or_publishes_policy(tmp_path):
    root, _ = stream(tmp_path)

    async def scenario():
        events = []
        gate = AdmissionGate()
        adapter = FakeAdapter(events)

        async def refuse(*args):
            raise RuntimeError("initial byte audit refused")

        adapter.initialize = refuse
        syncer = SparseWeightSyncer(root, adapter, gate, reload=lambda _: None)

        async def launch(directory):
            events.append(("launch", directory.name))

        with pytest.raises(RuntimeError, match="byte audit"):
            await syncer.bootstrap(launch)
        assert events == [("launch", "v000000")]
        assert not gate.healthy and gate.version is None

    asyncio.run(scenario())


def test_legacy_adapter_without_initialize_keeps_original_bootstrap_behavior(tmp_path):
    root, _ = stream(tmp_path)

    async def scenario():
        events = []
        gate = AdmissionGate()

        class LegacyAdapter:
            async def apply(self, directory, manifest):
                events.append(("apply", manifest.version))

            async def reset_cache(self):
                events.append(("cache", None))

        syncer = WeightSyncer(root, LegacyAdapter(), gate, reload=lambda _: None)

        async def launch(directory):
            events.append(("launch", directory.name))

        await syncer.bootstrap(launch)
        assert events == [
            ("launch", "v000000"),
            ("apply", 1),
            ("apply", 2),
            ("cache", None),
        ]
        assert gate.healthy and gate.version == 2

    asyncio.run(scenario())
