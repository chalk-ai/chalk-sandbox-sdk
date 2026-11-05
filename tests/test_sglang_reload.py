from __future__ import annotations

import asyncio
import argparse
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


def test_exact_upstream_provenance_and_no_rust_build_mode_allowed(stream, monkeypatch):
    root, _ = stream
    monkeypatch.setattr(
        "chalkcompute.weights.serving.adapters.sglang.version", lambda _: "0.5.23"
    )
    for key, value in {
        "SGLANG_BUILD_COMMIT": "297e54fd105e974cdf401e5b4d20253a22c16f90",
        "SGLANG_BUILD_URL": "https://github.com/sgl-project/sglang/actions/runs/40978390925",
        "SGLANG_IMAGE_TAG": "lmsysorg/sglang:v0.5.23-runtime",
        "SGLANG_RUST_BUILD_MODE": "never",
    }.items():
        monkeypatch.setenv(key, value)
    assert "sglang.launch_server" in SGLangAdapter().launch_cmd(
        root / version_dir(0), 8001
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("SGLANG_BUILD_COMMIT", "main"),
        ("SGLANG_BUILD_COMMIT", "297e54fd105e974cdf401e5b4d20253a22c16f90\n"),
        ("SGLANG_BUILD_URL", "https://example.com/build"),
        (
            "SGLANG_BUILD_URL",
            "https://github.com/sgl-project/sglang/actions/runs/40978390925?override=1",
        ),
        ("SGLANG_IMAGE_TAG", "lmsysorg/sglang:latest"),
        ("SGLANG_RUST_BUILD_MODE", "auto"),
        ("SGLANG_RUST_BUILD_MODE", "never "),
        ("SGLANG_BUILD_COMMIT_EXTRA", "anything"),
        ("SGLANG_ENABLE_WEIGHT_CACHE", "1"),
        ("SGLANG_ENABLE_WEIGHT_CACHE", "0"),
        ("SGLANG_OPT_BF16_FP32_GEMM_ALGO", "hpc"),
    ],
)
def test_provenance_allowlist_cannot_enable_runtime_overrides(
    stream, monkeypatch, key, value
):
    root, _ = stream
    monkeypatch.setattr(
        "chalkcompute.weights.serving.adapters.sglang.version", lambda _: "0.5.23"
    )
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match="environment"):
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


def test_shutdown_stops_native_group_before_joining_blocked_preparation(
    monkeypatch, tmp_path
):
    from chalkcompute.weights.serving import __main__ as entrypoint

    release = threading.Event()
    events = []
    outputs = []

    async def run():
        exited = asyncio.Event()
        loop = asyncio.get_running_loop()

        class Process:
            pid = 12345
            returncode = None

            async def wait(self):
                await exited.wait()
                return 0

        async def spawn(*args, **kwargs):
            return Process()

        class Adapter:
            url = "http://127.0.0.1:8001"

            def __init__(self, *args, **kwargs):
                pass

            def launch_cmd(self, *args):
                return ["test-engine"]

            async def ready(self):
                return True

        class Syncer(FullCheckpointSyncer):
            async def bootstrap(self, launch):
                await launch(tmp_path)
                self.gate.version = 0

            async def run(self):
                # Simulate a blocked immutable mount read. Only engine stop
                # releases it; gathering cancellation first would deadlock.
                def prepare(root, plan, output):
                    outputs.append(output)
                    loop.call_soon_threadsafe(self.gate.failed.set)
                    if not release.wait(5):
                        raise TimeoutError("Engine was not stopped before writer join")
                    events.append("writer-joined")
                    (output / "finished").write_text("done")

                monkeypatch.setattr(
                    "chalkcompute.weights.serving.full_checkpoint.materialize", prepare
                )
                await self._prepare(ApplyPlan(0, ()))

            def close(self):
                assert exited.is_set() and events == ["engine-stop", "writer-joined"]
                assert not outputs[0].exists()
                events.append("checkpoint-cleanup")
                super().close()

        class Runner:
            def __init__(self, *args, **kwargs):
                pass

            async def setup(self):
                pass

            async def cleanup(self):
                assert events[-1] == "checkpoint-cleanup"
                events.append("http-cleanup")

        class Site:
            def __init__(self, *args, **kwargs):
                pass

            async def start(self):
                pass

        def stop_engine(pid, signum):
            events.append("engine-stop")
            exited.set()
            release.set()

        monkeypatch.setattr(entrypoint, "SGLangAdapter", Adapter)
        monkeypatch.setattr(entrypoint, "FullCheckpointSyncer", Syncer)
        monkeypatch.setattr(entrypoint.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(entrypoint.web, "AppRunner", Runner)
        monkeypatch.setattr(entrypoint.web, "TCPSite", Site)
        monkeypatch.setattr(entrypoint.os, "killpg", stop_engine)
        args = argparse.Namespace(
            engine="sglang-experimental",
            weights=str(tmp_path),
            engine_port=8001,
            engine_args=[],
            startup_timeout=1,
        )
        with pytest.raises(RuntimeError, match="became unhealthy"):
            await entrypoint.serve(args)
        assert events == [
            "engine-stop",
            "writer-joined",
            "checkpoint-cleanup",
            "http-cleanup",
        ]

    try:
        asyncio.run(run())
    finally:
        release.set()


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "pause",
        "reload",
        "paused_requests",
        "flush",
        "resume",
        "non_json",
        "version",
        "model_path",
        "world_size",
    ],
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

        async def native_identity(request):
            if request.path == "/model_info":
                return web.json_response(
                    {
                        "model_path": "wrong"
                        if failure == "model_path"
                        else str(root / version_dir(0)),
                        "load_format": "safetensors",
                        "weight_version": "1" if failure == "version" else "2",
                    }
                )
            result = runtime_info()
            if failure == "world_size":
                result["internal_states"][0]["world_size"] = 2
            return web.json_response(result)

        app = web.Application()
        app.router.add_post("/{path}", control)
        app.router.add_get("/{path}", native_identity)
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
            if failure in (
                "pause",
                "reload",
                "paused_requests",
                "flush",
                "non_json",
                "version",
                "model_path",
                "world_size",
            ):
                assert "/continue_generation" not in [path for path, _ in calls]

    asyncio.run(run())


def test_deterministic_triton_is_explicit_diagnostic_profile_only(
    tmp_path, monkeypatch
):
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "architectures": ["LlamaForCausalLM"],
                "model_type": "llama",
                "dtype": "bfloat16",
            }
        )
    )
    monkeypatch.setattr(
        "chalkcompute.weights.serving.adapters.sglang.version", lambda _: "0.5.23"
    )
    default = SGLangAdapter().launch_cmd(tmp_path, 8001)
    assert "--enable-deterministic-inference" not in default
    assert "--attention-backend" not in default
    diagnostic = SGLangAdapter(diagnostic_profile="deterministic-triton").launch_cmd(
        tmp_path, 8001
    )
    assert diagnostic.count("--enable-deterministic-inference") == 1
    assert diagnostic[diagnostic.index("--attention-backend") + 1] == "triton"
    assert "--disable-cuda-graph" in diagnostic
    for profile in ("flashinfer", "deterministic", "DETERMINISTIC-TRITON", True):
        with pytest.raises(ValueError, match="diagnostic profile"):
            SGLangAdapter(diagnostic_profile=profile)  # ty: ignore[invalid-argument-type]
    for options in (
        ["--enable-deterministic-inference"],
        ["--attention-backend", "triton"],
        ["--sampling-backend", "pytorch"],
    ):
        for profile in (None, "deterministic-triton"):
            with pytest.raises(ValueError, match="Unsupported"):
                SGLangAdapter(engine_args=options, diagnostic_profile=profile)


def runtime_info():
    config = {
        "dtype": "bfloat16",
        "weight_cache_mode": "off",
        "disable_cuda_graph": True,
        "enable_deterministic_inference": True,
        "attention_backend": "triton",
        "sampling_backend": "pytorch",
        "disable_radix_cache": False,
    }
    return {**config, "internal_states": [{**config, "world_size": 1}]}


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "missing",
        "conflict",
        "parallel",
        "bool_world",
        "two_workers",
        "model_path",
        "format",
        "oversize",
    ],
)
def test_readiness_requires_actual_managed_native_contract(stream, failure):
    root, _ = stream
    model_dir = root / version_dir(0)

    async def run():
        async def get(request):
            if request.path == "/health":
                return web.Response()
            if request.path == "/model_info":
                return web.json_response(
                    {
                        "model_path": "wrong"
                        if failure == "model_path"
                        else str(model_dir),
                        "load_format": "pt" if failure == "format" else "safetensors",
                    }
                )
            info = runtime_info()
            if failure == "missing":
                for config in (info, info["internal_states"][0]):
                    config.pop("attention_backend")
            if failure == "conflict":
                info["sampling_backend"] = "flashinfer"
            if failure == "parallel":
                info["internal_states"][0]["world_size"] = 2
            if failure == "bool_world":
                info["internal_states"][0]["world_size"] = True
            if failure == "two_workers":
                info["internal_states"] *= 2
            if failure == "oversize":
                return web.Response(body=b"x" * (256 * 1024 + 1))
            # Actual chunked transport ensures bounded reads do not assume a
            # single read returns the complete response.
            body = json.dumps(info).encode()
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            await response.prepare(request)
            await response.write(body[:20])
            await asyncio.sleep(0)
            await response.write(body[20:])
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_get("/{path}", get)
        async with TestServer(app) as server:
            adapter = SGLangAdapter(diagnostic_profile="deterministic-triton")
            adapter._model_dir = model_dir
            adapter.url = str(server.make_url("")).rstrip("/")
            if failure:
                with pytest.raises(RuntimeError):
                    await adapter.ready()
            else:
                assert await adapter.ready()

    asyncio.run(run())


def test_cli_exposes_only_reviewed_sglang_profile(monkeypatch, stream):
    import sys
    from chalkcompute.weights.serving import __main__ as entrypoint

    captured = []

    async def fake_serve(args):
        captured.append(args)

    monkeypatch.setattr(entrypoint, "serve", fake_serve)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "chalk-live-serve",
            "--engine",
            "sglang-experimental",
            "--sglang-profile",
            "deterministic-triton",
            "--weights",
            str(stream[0]),
        ],
    )
    entrypoint.main()
    assert captured[0].engine == "sglang-experimental"
    assert captured[0].sglang_profile == "deterministic-triton"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "chalk-live-serve",
            "--engine",
            "sglang-experimental",
            "--sglang-profile",
            "flashinfer",
            "--weights",
            str(stream[0]),
        ],
    )
    with pytest.raises(SystemExit):
        entrypoint.main()


def test_unknown_engine_and_misplaced_profile_refused_before_native_launch(stream):
    from chalkcompute.weights.serving import __main__ as entrypoint

    for values in (
        {"engine": "unknown"},
        {"engine": "vllm", "sglang_profile": "deterministic-triton"},
    ):
        args = argparse.Namespace(
            engine_port=8001, engine_args=[], weights=str(stream[0]), **values
        )
        with pytest.raises(ValueError):
            asyncio.run(entrypoint.serve(args))
