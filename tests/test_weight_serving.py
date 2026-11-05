from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from chalkcompute._weights_codec import checksum
from chalkcompute._weights_format import (
    TensorEntry,
    WeightManifest,
    pointer_json,
    version_dir,
)
from chalkcompute.weights.serving.adapters.vllm import VLLMAdapter
from chalkcompute.weights.serving.supervisor import (
    AdmissionGate,
    DrainTimeout,
    create_app,
)
from chalkcompute.weights.serving.syncer import ApplyError, StreamError, WeightSyncer


class FakeAdapter:
    def __init__(self) -> None:
        self.applied: list[int] = []
        self.reset = 0
        self.fail = False

    async def apply(self, root, manifest):
        self.applied.append(manifest.version)
        if self.fail:
            raise RuntimeError("device write failed")

    async def reset_cache(self):
        self.reset += 1

    async def ready(self):
        return True

    def launch_cmd(self, model_dir, port):
        return []


def put(root: Path, version: int, parent: int | None, *, keyframe=False, arch="a" * 64):
    directory = root / version_dir(version)
    directory.mkdir()
    payload = f"tensor-at-{version}".encode()
    (directory / "tensor").write_bytes(payload)
    entry = TensorEntry(
        "weight",
        "float32",
        (2,),
        "dense",
        "tensor",
        0,
        len(payload),
        2,
        checksum(payload),
    )
    manifest = WeightManifest(
        version, parent, "keyframe" if keyframe else "patch", "test", arch, (entry,)
    )
    (directory / "manifest.json").write_bytes(manifest.to_json())
    (root / "CURRENT").write_bytes(pointer_json(version))
    return manifest


def test_gate_drains_before_update_and_holds_new_admissions():
    async def run():
        gate = AdmissionGate()
        gate.version = 1
        entered = asyncio.Event()
        release = asyncio.Event()
        updated = asyncio.Event()
        versions = []

        async def old_request():
            async with gate.admit() as version:
                versions.append(version)
                entered.set()
                await release.wait()

        async def update():
            async with gate.update(1):
                gate.version = 2
                updated.set()

        async def new_request():
            async with gate.admit() as version:
                versions.append(version)

        old = asyncio.create_task(old_request())
        await entered.wait()
        updating = asyncio.create_task(update())
        await asyncio.sleep(0)
        new = asyncio.create_task(new_request())
        await asyncio.sleep(0)
        assert not updated.is_set()
        assert versions == [1]
        release.set()
        await asyncio.gather(old, updating, new)
        assert versions == [1, 2]

    asyncio.run(run())


def test_drain_timeout_aborts_update_and_releases_gate():
    async def run():
        gate = AdmissionGate()
        gate.version = 3
        async with gate.admit():
            with pytest.raises(DrainTimeout):
                async with gate.update(0.01):
                    pytest.fail("Must not mutate weights while request is active")
        async with gate.admit() as version:
            assert version == 3

    asyncio.run(run())


def test_cancelled_update_releases_admission():
    async def run():
        gate = AdmissionGate()
        gate.version = 2
        async with gate.admit():

            async def update():
                async with gate.update(30):
                    pytest.fail("Still draining")

            task = asyncio.create_task(update())
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        async with gate.admit() as version:
            assert version == 2

    asyncio.run(run())


def test_pin_rollback_replays_keyframe_at_or_before_target(tmp_path):
    put(tmp_path, 0, None, keyframe=True)
    put(tmp_path, 1, 0)
    put(tmp_path, 2, 1)
    put(tmp_path, 3, 2, keyframe=True)
    (tmp_path / "PIN").write_bytes(pointer_json(1))

    async def run():
        gate = AdmissionGate()
        gate.version = 3
        adapter = FakeAdapter()
        syncer = WeightSyncer(tmp_path, adapter, gate)
        assert await syncer.sync_once()
        assert adapter.applied == [0, 1]
        assert gate.version == 1
        assert adapter.reset == 1

    asyncio.run(run())


def test_lag_and_gap_use_compatible_keyframe(tmp_path):
    put(tmp_path, 0, None, keyframe=True)
    put(tmp_path, 4, 3, keyframe=True)
    put(tmp_path, 5, 4)

    async def run():
        gate = AdmissionGate()
        gate.version = 1
        adapter = FakeAdapter()
        syncer = WeightSyncer(tmp_path, adapter, gate, keyframe_lag=2)
        assert await syncer.sync_once()
        assert adapter.applied == [4, 5]

    asyncio.run(run())


def test_missing_parent_refuses_update(tmp_path):
    put(tmp_path, 0, None, keyframe=True)
    put(tmp_path, 2, 1)
    gate = AdmissionGate()
    gate.version = 0
    adapter = FakeAdapter()
    syncer = WeightSyncer(tmp_path, adapter, gate)
    with pytest.raises(StreamError, match="Missing manifest"):
        asyncio.run(syncer.sync_once())
    assert adapter.applied == []
    assert gate.healthy


def test_later_checksum_failure_prevents_all_device_writes(tmp_path):
    put(tmp_path, 0, None, keyframe=True)
    put(tmp_path, 1, 0)
    put(tmp_path, 2, 1)
    (tmp_path / version_dir(2) / "tensor").write_bytes(b"x" * len(b"tensor-at-2"))
    gate = AdmissionGate()
    gate.version = 0
    adapter = FakeAdapter()
    with pytest.raises(Exception, match="Checksum"):
        asyncio.run(WeightSyncer(tmp_path, adapter, gate).sync_once())
    assert adapter.applied == []
    assert gate.version == 0
    assert gate.healthy


def test_architecture_mismatch_prevents_device_writes(tmp_path):
    put(tmp_path, 1, None, keyframe=True, arch="b" * 64)
    gate = AdmissionGate()
    gate.version = 0
    adapter = FakeAdapter()
    syncer = WeightSyncer(tmp_path, adapter, gate)
    syncer.architecture_hash = "a" * 64
    with pytest.raises(StreamError, match="architecture"):
        asyncio.run(syncer.sync_once())
    assert adapter.applied == []


def test_apply_failure_fails_closed_and_never_admits(tmp_path):
    put(tmp_path, 0, None, keyframe=True)
    put(tmp_path, 1, 0)

    async def run():
        gate = AdmissionGate()
        gate.version = 0
        adapter = FakeAdapter()
        adapter.fail = True
        with pytest.raises(ApplyError):
            await WeightSyncer(tmp_path, adapter, gate).sync_once()
        assert not gate.healthy
        assert gate.failed.is_set()
        with pytest.raises(RuntimeError, match="not ready"):
            async with gate.admit():
                pytest.fail("Partial weights must not serve")

    asyncio.run(run())


def test_bootstrap_loads_keyframe_once_then_replays(tmp_path):
    put(tmp_path, 0, None, keyframe=True)
    put(tmp_path, 1, 0)

    async def run():
        gate = AdmissionGate()
        adapter = FakeAdapter()
        launched = []

        async def launch(path):
            launched.append(path.name)
            assert gate.version is None

        await WeightSyncer(tmp_path, adapter, gate).bootstrap(launch)
        assert launched == ["v000000"]
        assert adapter.applied == [1]
        assert gate.version == 1

    asyncio.run(run())


def test_http_stream_stamps_admission_and_blocks_controls():
    async def run():
        started = asyncio.Event()
        release = asyncio.Event()

        async def generate(request):
            response = web.StreamResponse(
                headers={"x-chalk-policy-version": "untrusted"}
            )
            await response.prepare(request)
            await response.write(b"data: first\n\n")
            started.set()
            await release.wait()
            await response.write(b"data: second\n\n")
            return response

        engine = web.Application()
        engine.router.add_post("/v1/chat/completions", generate)
        async with TestServer(engine) as upstream:
            gate = AdmissionGate()
            gate.version = 7
            async with TestServer(
                create_app(gate, str(upstream.make_url("")))
            ) as public:
                async with ClientSession() as client:
                    async with client.get(public.make_url("/chalk/health")) as health:
                        assert health.status == 200
                        assert health.headers["x-chalk-weight-version"] == "7"
                    async with client.post(
                        public.make_url("/collective_rpc"), json={}
                    ) as blocked:
                        assert blocked.status == 404
                    response = await client.post(
                        public.make_url("/v1/chat/completions"), json={}
                    )
                    await started.wait()
                    assert response.headers["x-chalk-policy-version"] == "7"
                    with pytest.raises(DrainTimeout):
                        async with gate.update(0.01):
                            pytest.fail("SSE response is still active")
                    release.set()
                    body = await response.read()
                    assert body == b"data: first\n\ndata: second\n\n"
                    response.release()
                    async with gate.update(1):
                        gate.version = 8

    asyncio.run(run())


def test_adapter_rejects_bad_rank_ack_and_unsafe_args(tmp_path):
    adapter = VLLMAdapter(engine_args=["--host=0.0.0.0"])
    with pytest.raises(ValueError, match="managed"):
        adapter.launch_cmd(tmp_path, 8001)
    manifest = put(tmp_path, 0, None, keyframe=True)

    class BadAdapter(VLLMAdapter):
        async def _post(self, path, body):
            assert body["args"] == [str(tmp_path), "0"]
            assert all(isinstance(arg, (str, int)) for arg in body["args"])
            return {
                "results": [{"ok": True, "version": 0}, {"ok": False, "version": 0}]
            }

    with pytest.raises(RuntimeError, match="acknowledged"):
        asyncio.run(BadAdapter(engine_args=["-tp", "2"]).apply(tmp_path, manifest))


def test_disconnect_drains_native_eof_before_another_update():
    async def run():
        started = asyncio.Event()
        release = asyncio.Event()

        async def generate(request):
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(b"first")
            started.set()
            await release.wait()
            # More than the bounded proxy queue: a disconnected consumer must
            # never leave a producer blocked on queue.put().
            for _ in range(32):
                await response.write(b"x" * (64 * 1024))
            return response

        engine = web.Application()
        engine.router.add_post("/v1/completions", generate)
        async with TestServer(engine) as upstream:
            gate = AdmissionGate()
            gate.version = 1
            public = TestServer(create_app(gate, str(upstream.make_url(""))))
            await public.start_server()
            try:
                async with ClientSession() as client:
                    response = await client.post(
                        public.make_url("/v1/completions"), json={}
                    )
                    await started.wait()
                    response.close()
                    applied = asyncio.Event()

                    async def update():
                        async with gate.update(1):
                            applied.set()

                    updater = asyncio.create_task(update())
                    await asyncio.sleep(0.02)
                    assert gate.healthy
                    assert gate._active == 1
                    assert not applied.is_set()
                    release.set()
                    await asyncio.wait_for(updater, 1)
                    assert applied.is_set()
                    assert gate.healthy
                    assert gate._active == 0
            finally:
                release.set()
                await public.close()

    asyncio.run(run())


@pytest.mark.parametrize("timeout", [False, True])
def test_uncertain_native_completion_fails_closed(timeout):
    async def run():
        release = asyncio.Event()

        async def generate(request):
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(b"first")
            if timeout:
                await release.wait()
            else:
                request.transport.close()
            return response

        engine = web.Application()
        engine.router.add_post("/v1/completions", generate)
        async with TestServer(engine) as upstream:
            gate = AdmissionGate()
            gate.version = 1
            public = TestServer(
                create_app(
                    gate,
                    str(upstream.make_url("")),
                    request_timeout=0.05 if timeout else None,
                )
            )
            await public.start_server()
            try:
                async with ClientSession() as client:
                    response = await client.post(
                        public.make_url("/v1/completions"), json={}
                    )
                    await asyncio.wait_for(gate.failed.wait(), 1)
                    assert not gate.healthy
                    assert "POST /v1/completions" in gate.failure_reason
                    assert (
                        "TimeoutError" if timeout else "ClientPayloadError"
                    ) in gate.failure_reason
                    with pytest.raises(RuntimeError, match="unhealthy"):
                        async with gate.update(1):
                            pytest.fail(
                                "Uncertain native completion must block updates"
                            )
                    response.close()
            finally:
                release.set()
                await public.close()

    asyncio.run(run())


def test_serving_shutdown_stops_engine_before_app_drain_cleanup(monkeypatch, tmp_path):
    import argparse

    from chalkcompute.weights.serving import __main__ as entrypoint

    async def run():
        events = []
        exited = asyncio.Event()

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

            def __init__(self, *args):
                pass

            def launch_cmd(self, *args):
                return ["test-engine"]

            async def ready(self):
                return True

        class Syncer:
            def __init__(self, root, adapter, gate, **kwargs):
                self.gate = gate

            async def bootstrap(self, launch):
                await launch(tmp_path)

            async def run(self):
                await self.gate.fail("test native failure")
                await asyncio.Event().wait()

        class Runner:
            def __init__(self, *args, **kwargs):
                pass

            async def setup(self):
                pass

            async def cleanup(self):
                assert exited.is_set()
                events.append("drain-cleanup")

        class Site:
            def __init__(self, *args):
                pass

            async def start(self):
                pass

        def stop_engine(pid, signum):
            events.append("engine-stop")
            exited.set()

        monkeypatch.setattr(entrypoint, "VLLMAdapter", Adapter)
        monkeypatch.setattr(entrypoint, "WeightSyncer", Syncer)
        monkeypatch.setattr(entrypoint.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(entrypoint.web, "AppRunner", Runner)
        monkeypatch.setattr(entrypoint.web, "TCPSite", Site)
        monkeypatch.setattr(entrypoint.os, "killpg", stop_engine)
        args = argparse.Namespace(
            weights=str(tmp_path), engine_port=8001, engine_args=[], startup_timeout=1
        )
        with pytest.raises(RuntimeError, match="test native failure"):
            await entrypoint.serve(args)
        assert events == ["engine-stop", "drain-cleanup"]

    asyncio.run(run())


def test_cancel_native_with_full_queue_and_attached_consumer(monkeypatch):
    from chalkcompute.weights.serving import supervisor

    async def run():
        full = asyncio.Event()
        stalled = asyncio.Event()

        class Upstream:
            status = 200
            headers = {}

            @property
            def content(self):
                return self

            async def iter_chunked(self, size):
                for index in range(100):
                    if index == 9:
                        full.set()
                    yield b"x" * size

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class Session:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def request(self, *args, **kwargs):
                return Upstream()

        class Response:
            prepared = True

            def __init__(self, **kwargs):
                pass

            async def prepare(self, request):
                pass

            async def write(self, chunk):
                await stalled.wait()

        class Request:
            method = "POST"
            path = rel_url = "/v1/completions"
            headers = {}
            content = None

        request = Request()
        request.content = request
        request.iter_any = lambda: None
        monkeypatch.setattr(supervisor, "ClientSession", Session)
        monkeypatch.setattr(supervisor.web, "StreamResponse", Response)
        gate = AdmissionGate()
        gate.version = 0
        app = create_app(gate, "http://engine")
        clients = app.cleanup_ctx[0](app)
        await clients.__anext__()
        handler = asyncio.create_task(list(app.router.routes())[-1].handler(request))
        try:
            await asyncio.wait_for(full.wait(), 1)
            native = next(
                task
                for task in asyncio.all_tasks()
                if task.get_name() == "inference-native-drain"
            )
            native.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(native, 1)
            assert not handler.done()  # downstream is still attached and stalled
            assert not gate.healthy
            assert gate._active == 0
        finally:
            handler.cancel()
            await asyncio.gather(handler, return_exceptions=True)
            await clients.aclose()

    asyncio.run(run())


def test_vllm_adapter_uses_native_http_envelope_and_reset_failure(tmp_path):
    manifest = put(tmp_path, 0, None, keyframe=True)

    async def run():
        calls = []

        async def rpc(request):
            body = await request.json()
            calls.append(body)
            return web.json_response({"results": [{"ok": True, "version": 0}]})

        async def reset(request):
            return web.json_response({"success": False})

        engine = web.Application()
        engine.router.add_post("/collective_rpc", rpc)
        engine.router.add_post("/reset_prefix_cache", reset)
        async with TestServer(engine) as server:
            adapter = VLLMAdapter()
            adapter.url = str(server.make_url("")).rstrip("/")
            await adapter.apply(tmp_path, manifest)
            assert calls == [{"method": "ws_apply_dir", "args": [str(tmp_path), "0"]}]
            with pytest.raises(RuntimeError, match="failed"):
                await adapter.reset_cache()

    asyncio.run(run())


def test_adapter_rejects_missing_tensor_parallel_rank(tmp_path):
    manifest = put(tmp_path, 0, None, keyframe=True)

    class MissingRank(VLLMAdapter):
        async def _post(self, path, body):
            return {"results": [{"ok": True, "version": 0}]}

    with pytest.raises(RuntimeError, match="acknowledgements"):
        asyncio.run(
            MissingRank(engine_args=["--tensor-parallel-size", "2"]).apply(
                tmp_path, manifest
            )
        )


def test_keyframe_metrics_count_shared_safetensors_file_once(tmp_path):
    from dataclasses import replace

    manifest = put(tmp_path, 0, None, keyframe=True)
    shared = replace(manifest.tensors[0], name="other_weight")
    manifest = replace(manifest, tensors=(*manifest.tensors, shared))
    (tmp_path / version_dir(0) / "manifest.json").write_bytes(manifest.to_json())

    async def run():
        gate = AdmissionGate()
        gate.version = 3
        syncer = WeightSyncer(tmp_path, FakeAdapter(), gate)
        await syncer.sync_once()
        assert syncer.metrics.bytes == manifest.tensors[0].length

    asyncio.run(run())


def test_patch_density_includes_unchanged_model_tensors(tmp_path):
    from dataclasses import replace
    from chalkcompute.weights.serving.supervisor import Metrics

    base = put(tmp_path, 0, None, keyframe=True)
    untouched = replace(base.tensors[0], name="unchanged", shape=(98,), nnz=98)
    base = replace(base, tensors=(*base.tensors, untouched))
    (tmp_path / version_dir(0) / "manifest.json").write_bytes(base.to_json())

    async def run():
        gate = AdmissionGate()
        metrics = Metrics()
        syncer = WeightSyncer(tmp_path, FakeAdapter(), gate, metrics=metrics)

        async def launch(path):
            pass

        await syncer.bootstrap(launch)
        patch = put(tmp_path, 1, 0)
        patch = replace(
            patch, tensors=(replace(patch.tensors[0], encoding="sparse", nnz=1),)
        )
        (tmp_path / version_dir(1) / "manifest.json").write_bytes(patch.to_json())
        await syncer.sync_once()
        assert metrics.density == 0.01

    asyncio.run(run())
