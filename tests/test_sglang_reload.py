from __future__ import annotations

import asyncio
import dataclasses
import json
import threading

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from chalkcompute._weights_codec import TensorPatch, checksum, encode_patch
from chalkcompute._weights_format import (
    TensorEntry,
    WeightManifest,
    WeightStreamError,
    architecture_hash,
    pointer_json,
    version_dir,
)
from chalkcompute.weights.serving.adapters.sglang import SGLangAdapter, validate_config
from chalkcompute.weights.serving.full_checkpoint import (
    FullCheckpointSyncer,
    materialize,
)
from chalkcompute.weights.serving.supervisor import AdmissionGate, UnhealthyError
from chalkcompute.weights.serving.syncer import ApplyError, ApplyPlan


@pytest.fixture
def stream(tmp_path):
    torch = pytest.importorskip("torch")
    st = pytest.importorskip("safetensors.torch")
    config = json.dumps(
        {
            "architectures": ["LlamaForCausalLM"],
            "model_type": "llama",
            "torch_dtype": "bfloat16",
        }
    ).encode()
    state = {
        "layer.weight": torch.arange(4, dtype=torch.bfloat16),
        "lm_head.weight": torch.zeros(2, dtype=torch.bfloat16),
    }
    architecture = architecture_hash(config, state.items())
    root = tmp_path / "stream"
    root.mkdir()
    directory = root / version_dir(0)
    directory.mkdir()
    (directory / "config.json").write_bytes(config)
    (directory / "tokenizer.json").write_text("{}")
    (directory / "model.safetensors.index.json").write_text("{}")
    st.save_file(state, str(directory / "weights.safetensors"))
    payload = (directory / "weights.safetensors").read_bytes()
    entries = tuple(
        TensorEntry(
            name,
            "bfloat16",
            tuple(tensor.shape),
            "dense",
            "weights.safetensors",
            0,
            len(payload),
            tensor.numel(),
            checksum(payload),
        )
        for name, tensor in state.items()
    )
    manifests = [WeightManifest(0, None, "keyframe", "test", architecture, entries)]
    for version in (1, 2):
        directory = root / version_dir(version)
        directory.mkdir()
        patch = TensorPatch(
            "layer.weight",
            (4,),
            "bfloat16",
            torch.tensor([10 + version], dtype=torch.bfloat16),
            torch.tensor([version - 1]),
        )
        payload = encode_patch(patch)
        (directory / "patches.bin").write_bytes(payload)
        entry = TensorEntry(
            "layer.weight",
            "bfloat16",
            (4,),
            "sparse",
            "patches.bin",
            0,
            len(payload),
            1,
            checksum(payload),
        )
        manifests.append(
            WeightManifest(
                version, version - 1, "patch", "test", architecture, (entry,)
            )
        )
    for manifest in manifests:
        (root / version_dir(manifest.version) / "manifest.json").write_bytes(
            manifest.to_json()
        )
    (root / "CURRENT").write_bytes(pointer_json(2))
    return root, manifests


def test_replay_produces_one_complete_final_checkpoint(stream, tmp_path):
    from safetensors.torch import load_file

    root, manifests = stream
    output = tmp_path / "output"
    output.mkdir()
    materialize(root, ApplyPlan(2, tuple(manifests)), output)
    state = load_file(str(output / "model.safetensors"))
    assert state["layer.weight"].tolist() == [11, 12, 2, 3]
    assert state["lm_head.weight"].tolist() == [0, 0]
    assert (output / "tokenizer.json").read_text() == "{}"
    assert not (output / "model.safetensors.index.json").exists()
    assert not (output / "patches.bin").exists()


def test_actual_catalog_checked_before_native_mutation(stream, tmp_path):
    root, manifests = stream
    entries = (
        dataclasses.replace(manifests[0].tensors[0], shape=(2, 2)),
        *manifests[0].tensors[1:],
    )
    plan = ApplyPlan(0, (dataclasses.replace(manifests[0], tensors=entries),))
    with pytest.raises(WeightStreamError, match="catalog"):
        materialize(root, plan, tmp_path)


@pytest.mark.parametrize(
    "change",
    [
        {"quantization_config": {}},
        {"compression_config": {}},
        {"text_config": {}},
        {"torch_dtype": "float16"},
        {"model_type": "deepseek"},
        {"architectures": ["Qwen2MoeForCausalLM"]},
    ],
)
def test_unsupported_config_rejected(change):
    value = {
        "architectures": ["LlamaForCausalLM"],
        "model_type": "llama",
        "torch_dtype": "bfloat16",
        **change,
    }
    with pytest.raises(WeightStreamError):
        validate_config(json.dumps(value).encode())


@pytest.mark.parametrize(
    "args",
    [
        ["--tp-size", "2"],
        ["--ep-size=2"],
        ["--quantization", "fp8"],
        ["--enable-hierarchical-cache"],
        ["--model", "/other"],
        ["--mem", "0.8"],
        ["--context-length", "0"],
        ["--context-length"],
        ["--mem-fraction-static=nan"],
    ],
)
def test_unsupported_launch_options_rejected(args):
    with pytest.raises(ValueError):
        SGLangAdapter(engine_args=args)


def test_managed_launch_is_loopback_bf16_tp1(stream, monkeypatch):
    root, _ = stream
    monkeypatch.setattr(
        "chalkcompute.weights.serving.adapters.sglang.version", lambda _: "0.5.23"
    )
    adapter = SGLangAdapter(
        engine_args=["--context-length", "4096", "--mem-fraction-static=0.8"]
    )
    cmd = adapter.launch_cmd(root / version_dir(0), 8001)
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1"
    assert cmd[cmd.index("--dtype") + 1] == "bfloat16"
    assert cmd[cmd.index("--tp-size") + 1] == "1"
    assert "--disable-cuda-graph" in cmd
    monkeypatch.setenv("SGLANG_ENABLE_WEIGHT_CACHE", "1")
    with pytest.raises(ValueError, match="environment"):
        adapter.launch_cmd(root / version_dir(0), 8001)


def test_unreviewed_sglang_version_rejected(stream, monkeypatch):
    root, _ = stream
    monkeypatch.setattr(
        "chalkcompute.weights.serving.adapters.sglang.version", lambda _: "0.5.24"
    )
    with pytest.raises(ValueError, match="exactly"):
        SGLangAdapter().launch_cmd(root / version_dir(0), 8001)


class FakeDenseAdapter:
    def __init__(self, gate, *, fail=False):
        self.gate, self.fail = gate, fail
        self.calls = []

    async def apply(self, directory, manifest):
        from safetensors.torch import load_file

        assert self.gate._paused and self.gate._active == 0
        assert self.gate.version == 0
        assert load_file(str(directory / "model.safetensors"))[
            "layer.weight"
        ].tolist() == [11, 12, 2, 3]
        self.calls.append((directory, manifest.version))
        if self.fail:
            raise RuntimeError("resume response lost")


def test_one_reload_per_plan_outside_gate_and_version_after_resume(stream, monkeypatch):
    root, _ = stream
    started, release = threading.Event(), threading.Event()

    def prepare(*args):
        started.set()
        assert release.wait(5)
        materialize(*args)

    monkeypatch.setattr(
        "chalkcompute.weights.serving.full_checkpoint.materialize", prepare
    )

    async def run():
        gate = AdmissionGate()
        gate.version = 0
        adapter = FakeDenseAdapter(gate)
        syncer = FullCheckpointSyncer(root, adapter, gate)
        try:
            task = asyncio.create_task(syncer.sync_once())
            assert await asyncio.to_thread(started.wait, 5)
            async with gate.admit() as version:
                assert version == 0
            release.set()
            assert await task
            assert len(adapter.calls) == 1 and adapter.calls[0][1] == 2
            assert gate.version == 2 and gate.healthy
            assert not await syncer.sync_once()
            assert adapter.calls[0][0].exists()
        finally:
            syncer.close()
        assert not adapter.calls[0][0].exists()

    asyncio.run(run())


def test_failed_reload_fails_closed_and_keeps_native_checkpoint_until_shutdown(stream):
    root, _ = stream

    async def run():
        gate = AdmissionGate()
        gate.version = 0
        adapter = FakeDenseAdapter(gate, fail=True)
        syncer = FullCheckpointSyncer(root, adapter, gate)
        with pytest.raises(ApplyError):
            await syncer.sync_once()
        assert gate.version == 0 and not gate.healthy
        assert adapter.calls[0][0].exists()
        with pytest.raises(UnhealthyError):
            async with gate.admit():
                pass
        syncer.close()
        assert not adapter.calls[0][0].exists()

    asyncio.run(run())


def test_bad_final_payload_leaves_live_engine_untouched(stream):
    root, _ = stream
    (root / version_dir(2) / "patches.bin").write_bytes(b"corrupt")

    async def run():
        gate = AdmissionGate()
        gate.version = 0
        adapter = FakeDenseAdapter(gate)
        syncer = FullCheckpointSyncer(root, adapter, gate)
        with pytest.raises(WeightStreamError):
            await syncer.sync_once()
        assert adapter.calls == [] and gate.healthy and gate.version == 0

    asyncio.run(run())


def test_bootstrap_launches_final_checkpoint_and_retains_assets_until_shutdown(stream):
    from safetensors.torch import load_file

    root, _ = stream

    async def run():
        gate = AdmissionGate()
        adapter = FakeDenseAdapter(gate)
        syncer = FullCheckpointSyncer(root, adapter, gate)
        launched = []

        async def launch(path):
            assert gate.version is None
            assert load_file(str(path / "model.safetensors"))[
                "layer.weight"
            ].tolist() == [11, 12, 2, 3]
            launched.append(path)

        await syncer.bootstrap(launch)
        assert gate.version == 2 and adapter.calls == []
        assert launched[0].exists()
        assert not hasattr(syncer, "state")
        syncer.close()
        assert not launched[0].exists()

    asyncio.run(run())


def test_rollback_replays_final_target_and_bounds_checkpoint_lifetime(stream):
    from safetensors.torch import load_file

    root, _ = stream

    async def run():
        gate = AdmissionGate()
        adapter = FakeDenseAdapter(gate)
        paths = []

        async def apply(path, manifest):
            assert gate._paused and gate._active == 0
            values = load_file(str(path / "model.safetensors"))["layer.weight"].tolist()
            assert values == (
                [11, 12, 2, 3] if manifest.version == 2 else [11, 1, 2, 3]
            )
            paths.append(path)

        adapter.apply = apply
        syncer = FullCheckpointSyncer(root, adapter, gate)
        (root / "CURRENT").write_bytes(pointer_json(0))
        initial = []

        async def launch(path):
            initial.append(path)

        await syncer.bootstrap(launch)
        (root / "CURRENT").write_bytes(pointer_json(2))
        await syncer.sync_once()
        assert initial[0].exists() and paths[0].exists()
        (root / "PIN").write_bytes(pointer_json(1))
        await syncer.sync_once()
        assert gate.version == 1
        assert initial[0].exists() and paths[1].exists() and not paths[0].exists()
        syncer.close()
        assert all(not path.exists() for path in initial + paths)

    asyncio.run(run())


def test_preparation_cancellation_joins_writer_before_cleanup(stream, monkeypatch):
    root, manifests = stream
    started, release = threading.Event(), threading.Event()
    outputs = []

    def prepare(root, plan, output):
        outputs.append(output)
        started.set()
        assert release.wait(5)
        (output / "writer-finished").write_text("done")

    monkeypatch.setattr(
        "chalkcompute.weights.serving.full_checkpoint.materialize", prepare
    )

    async def run():
        gate = AdmissionGate()
        syncer = FullCheckpointSyncer(root, FakeDenseAdapter(gate), gate)
        task = asyncio.create_task(syncer._prepare(ApplyPlan(2, tuple(manifests))))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and outputs[0].exists()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not outputs[0].exists() and gate.healthy

    asyncio.run(run())


def test_native_cancellation_keeps_uncertain_path_and_fails_closed(stream):
    root, _ = stream

    async def run():
        gate = AdmissionGate()
        gate.version = 0
        started = asyncio.Event()
        adapter = FakeDenseAdapter(gate)

        async def apply(path, manifest):
            adapter.calls.append((path, manifest.version))
            started.set()
            await asyncio.Event().wait()

        adapter.apply = apply
        syncer = FullCheckpointSyncer(root, adapter, gate)
        task = asyncio.create_task(syncer.sync_once())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not gate.healthy and gate.version == 0 and adapter.calls[0][0].exists()
        syncer.close()
        assert not adapter.calls[0][0].exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure",
    [None, "pause", "reload", "paused_requests", "flush", "resume", "non_json"],
)
def test_native_http_protocol_is_strict(stream, failure):
    root, manifests = stream

    async def run():
        calls = []

        async def control(request):
            calls.append((request.path, await request.json()))
            if request.path == "/update_weights_from_disk":
                if failure == "non_json":
                    return web.Response(text="ok")
                return web.json_response(
                    {
                        "success": failure != "reload",
                        "num_paused_requests": 1 if failure == "paused_requests" else 0,
                    }
                )
            if request.path == "/flush_cache":
                return web.Response(
                    status=400 if failure == "flush" else 200, text="Cache flushed."
                )
            failed = failure == (
                "pause" if request.path == "/pause_generation" else "resume"
            )
            return web.json_response({"status": "failed" if failed else "ok"})

        app = web.Application()
        app.router.add_post("/{path}", control)
        async with TestServer(app) as server:
            adapter = SGLangAdapter()
            adapter.url = str(server.make_url("")).rstrip("/")
            if failure:
                with pytest.raises(Exception):
                    await adapter.apply(root / version_dir(0), manifests[-1])
            else:
                await adapter.apply(root / version_dir(0), manifests[-1])
                assert [path for path, _ in calls] == [
                    "/pause_generation",
                    "/update_weights_from_disk",
                    "/flush_cache",
                    "/continue_generation",
                ]
                assert calls[1][1]["is_async"] is False
                assert calls[1][1]["flush_cache"] is True
                assert calls[1][1]["keep_pause"] is True
                assert calls[1][1]["weight_version"] == "2"
            if failure in ("pause", "reload", "paused_requests", "flush", "non_json"):
                assert "/continue_generation" not in [path for path, _ in calls]

    asyncio.run(run())
