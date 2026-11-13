"""CPU contract tests; these do not substitute for HTTP/GPU integration."""

import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chalkcompute._weights_format import WeightManifest, TensorEntry
from chalkcompute.weights.serving.adapters import sglang_resident as adapter_module
from chalkcompute.weights.serving.adapters.sglang_resident import SGLangResidentAdapter
from chalkcompute.weights.serving import sglang_resident_server as server
from chalkcompute.weights.serving import sglang_resident_worker as worker

from chalkcompute.weights.layout import WeightLayout, WeightSchema, TensorSpec

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
    return SGLangResidentAdapter(
        *args,
        weight_layout=_LAYOUT_PATH,
        weight_layout_sha256=hashlib.sha256(_LAYOUT_RAW).hexdigest(),
        **kwargs,
    )


def request(operation="apply"):
    return {
        **(
            {
                "layout_json": _LAYOUT_RAW.decode(),
                "layout_file_sha256": hashlib.sha256(_LAYOUT_RAW).hexdigest(),
            }
            if operation == "bind"
            else {}
        ),
        "operation": operation,
        "directory": "/weights/v00000001",
        "version": 1,
        "manifest_sha256": "a" * 64,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"operation": "reload"},
        {"version": True},
        {"version": -1},
        {"directory": "relative"},
        {"directory": "x" * 4097},
        {"manifest_sha256": "z" * 64},
        {"manifest_sha256": "a" * 63},
        {"ack_path": "/tmp/ack"},
    ],
)
def test_control_fields_fail_closed(change):
    with pytest.raises(ValueError):
        worker.validate_control({**request(), **change})


def test_scheduler_hook_returns_real_ack_and_delegates_other_methods(monkeypatch):
    class Scheduler:
        def handle_rpc_request(self, req):
            return "native"

    ack = {"ok": True, "version": 3}
    monkeypatch.setattr(worker, "control", lambda scheduler, params: ack)
    worker.install_scheduler_hook(Scheduler, SimpleNamespace)
    response = Scheduler().handle_rpc_request(
        SimpleNamespace(method=worker.METHOD, parameters=request())
    )
    assert response.success is True and json.loads(response.message) == ack
    assert Scheduler().handle_rpc_request(SimpleNamespace(method="other")) == "native"
    with pytest.raises(RuntimeError, match="Duplicate"):
        worker.install_scheduler_hook(Scheduler, SimpleNamespace)


def test_native_errors_are_bounded_without_paths(monkeypatch):
    class Scheduler:
        def handle_rpc_request(self, req):
            return None

    def fail(*args):
        raise ValueError("secret /private/path")

    monkeypatch.setattr(worker, "control", fail)
    worker.install_scheduler_hook(Scheduler, SimpleNamespace)
    result = Scheduler().handle_rpc_request(
        SimpleNamespace(method=worker.METHOD, parameters=request())
    )
    assert result.success is False
    assert json.loads(result.message) == {"ok": False, "error_type": "ValueError"}


def args():
    return SimpleNamespace(
        tp_size=1,
        pp_size=1,
        dp_size=1,
        ep_size=1,
        dtype="bfloat16",
        load_format="safetensors",
        disable_cuda_graph=True,
        disable_radix_cache=True,
        disable_overlap_schedule=True,
        enable_deterministic_inference=True,
        attention_backend="triton",
        weight_cache_mode="off",
        trust_remote_code=False,
        quantization=None,
        speculative_algorithm=None,
        host="127.0.0.1",
        tokenizer_worker_num=1,
        model_path="/weights/v00000001",
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("tp_size", 2),
        ("pp_size", 2),
        ("dp_size", 2),
        ("ep_size", 2),
        ("disable_radix_cache", False),
        ("disable_overlap_schedule", False),
        ("disable_cuda_graph", False),
        ("quantization", "fp8"),
        ("trust_remote_code", True),
        ("speculative_algorithm", "EAGLE"),
        ("attention_backend", "flashinfer"),
        ("tokenizer_worker_num", 2),
        ("host", "0.0.0.0"),
        ("tp_size", True),
    ],
)
def test_native_resolved_profile_rejects_unsupported(key, value):
    config = args()
    setattr(config, key, value)
    with pytest.raises(ValueError):
        worker.check_server_args(config)


def test_busy_overlap_or_draft_scheduler_cannot_write():
    for extra in [
        {"enable_overlap": True},
        {"draft_worker": object()},
        {"is_fully_idle": lambda: False},
    ]:
        scheduler = SimpleNamespace(
            **(
                {
                    "server_args": args(),
                    "enable_overlap": False,
                    "draft_worker": None,
                    "is_fully_idle": lambda: True,
                }
                | extra
            )
        )
        with pytest.raises(ValueError):
            worker.check_scheduler(scheduler)


def test_bind_requires_full_initial_verification_then_apply_flushes(
    monkeypatch, tmp_path
):
    from chalkcompute.weights.serving import resident, resident_runtime

    events = []
    (tmp_path / "config.json").write_text("{}")
    scheduler = SimpleNamespace(
        server_args=args(),
        draft_worker=None,
        enable_overlap=False,
        is_fully_idle=lambda: True,
        flush_cache=lambda: events.append("flush") or True,
    )
    scheduler.server_args.model_path = str(tmp_path)
    model = SimpleNamespace(parameters=lambda: iter([SimpleNamespace(device="cuda:0")]))
    scheduler.tp_worker = SimpleNamespace(model_runner=SimpleNamespace(model=model))

    class Resident:
        version = 1
        identity_sha256 = "b" * 64

        def __init__(self, model, config, manifest, layout):
            self.layout_sha256 = layout.sha256
            self.schema_sha256 = layout.source_schema_sha256
            self.model = model
            events.append("construct")

        def verify_initial(self, directory):
            events.append("verify_initial")
            return {"ok": True}

        def apply(self, *args):
            events.append("apply")
            return {"ok": True}

    monkeypatch.setattr(resident_runtime, "check_cuda", lambda device: None)
    monkeypatch.setattr(resident_runtime, "check_runtime_sources", lambda kind: None)
    monkeypatch.setattr(resident, "ResidentWeights", Resident)
    monkeypatch.setattr(resident, "load_manifest", lambda *a: object())
    req = {**request("bind"), "directory": str(tmp_path)}
    assert worker.control(scheduler, req)["identity_sha256"] == "b" * 64
    assert events == ["construct", "verify_initial"]
    worker.control(scheduler, {**request("apply"), "directory": str(tmp_path)})
    assert events[-2:] == ["apply", "flush"]
    with pytest.raises(ValueError, match="already bound"):
        worker.control(scheduler, req)
    scheduler.tp_worker.model_runner.model = object()
    with pytest.raises(RuntimeError, match="model changed"):
        worker.control(scheduler, {**request("apply"), "directory": str(tmp_path)})


def test_scheduler_channel_serializes_and_poison_prevents_replay():
    async def run():
        channel = server.SchedulerChannel("ipc:///private")
        active = 0
        high = 0

        async def exchange(parameters):
            nonlocal active, high
            active += 1
            high = max(high, active)
            await asyncio.sleep(0)
            active -= 1
            return {"ok": True}

        channel.exchange = exchange
        await asyncio.gather(channel.call(request()), channel.call(request()))
        assert high == 1
        channel.exchange = AsyncMock(side_effect=TimeoutError)
        with pytest.raises(TimeoutError):
            await channel.call(request())
        with pytest.raises(RuntimeError, match="restart"):
            await channel.call(request())
        assert channel.exchange.await_count == 1

    asyncio.run(run())


def test_channel_cancellation_is_uncertain():
    async def run():
        channel = server.SchedulerChannel("ipc:///private")
        channel.exchange = AsyncMock(side_effect=asyncio.CancelledError)
        with pytest.raises(asyncio.CancelledError):
            await channel.call(request())
        assert channel.poisoned

    asyncio.run(run())


def test_exchange_uses_native_async_codec_and_dealer_bind(monkeypatch):
    events = []

    class Socket:
        def setsockopt(self, key, value):
            events.append(("option", key, value))

    socket = Socket()

    class Output:
        success = True
        message = '{"ok":true}'

    async def send(sock, message):
        assert sock is socket
        assert message.method == worker.METHOD
        assert message.parameters == request()
        events.append("send")

    async def receive(sock):
        assert sock is socket
        events.append("receive")
        return Output()

    def get_socket(context, kind, endpoint, bind):
        assert kind == 5 and endpoint == "ipc:///private" and bind is True
        return socket

    modules = {
        "zmq": SimpleNamespace(
            DEALER=5, LINGER=17, MAXMSGSIZE=22, RCVHWM=24, SNDHWM=23
        ),
        "zmq.asyncio": SimpleNamespace(Context=lambda: object()),
        "sglang.srt.managers.io_struct": SimpleNamespace(
            RpcReqInput=SimpleNamespace,
            RpcReqOutput=Output,
            async_sock_send=send,
            async_sock_recv=receive,
        ),
        "sglang.srt.utils.network": SimpleNamespace(get_zmq_socket=get_socket),
    }
    original = server.importlib.import_module
    monkeypatch.setattr(
        server.importlib,
        "import_module",
        lambda name: modules[name] if name in modules else original(name),
    )
    channel = server.SchedulerChannel("ipc:///private")
    assert asyncio.run(channel.call(request())) == {"ok": True}
    assert events[-2:] == ["send", "receive"]


def test_adapter_import_does_not_load_torch_or_engines():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import chalkcompute.weights.serving.adapters.sglang_resident; "
            "assert not any(x in sys.modules for x in ('torch','sglang','vllm','zmq','fastapi'))",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_private_http_route_bounds_and_validates_before_ipc(monkeypatch):
    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            self.status_code = status_code

    fake = ModuleType("fastapi")
    fake.HTTPException = HTTPException
    fake.Request = object
    monkeypatch.setitem(sys.modules, "fastapi", fake)
    app = SimpleNamespace(
        add_api_route=lambda path, function, methods: routes.append(
            (path, function, methods)
        )
    )
    routes = []
    channel = SimpleNamespace(call=AsyncMock(return_value={"ok": True}))
    server.install_route(app, channel)
    path, endpoint, methods = routes[0]
    assert path == server.CONTROL_PATH and methods == ["POST"]

    class Request:
        def __init__(self, host, chunks):
            self.client = SimpleNamespace(host=host)
            self.chunks = chunks

        async def stream(self):
            for chunk in self.chunks:
                yield chunk

    assert asyncio.run(
        endpoint(Request("127.0.0.1", [json.dumps(request()).encode()]))
    ) == {"ok": True}
    assert channel.call.await_count == 1
    for host, chunks, status in [
        ("192.0.2.1", [b"{}"], 403),
        ("127.0.0.1", [b"x" * (worker.MAX_CONTROL_BYTES + 1)], 413),
        ("127.0.0.1", [b"{bad"], 400),
        ("127.0.0.1", [b"{}"], 400),
    ]:
        with pytest.raises(HTTPException) as error:
            asyncio.run(endpoint(Request(host, chunks)))
        assert error.value.status_code == status
    assert channel.call.await_count == 1
    channel.call.side_effect = ValueError("private path")
    with pytest.raises(HTTPException) as error:
        asyncio.run(endpoint(Request("::1", [json.dumps(request()).encode()])))
    assert error.value.status_code == 503


def test_official_tokenizer_hook_handles_keyword_arguments(monkeypatch):
    modules = {}
    for name in [
        "sglang",
        "sglang.srt",
        "sglang.srt.entrypoints",
        "sglang.srt.entrypoints.engine",
        "sglang.srt.entrypoints.http_server",
    ]:
        modules[name] = ModuleType(name)
        monkeypatch.setitem(sys.modules, name, modules[name])
    modules["sglang.srt.entrypoints"].http_server = modules[
        "sglang.srt.entrypoints.http_server"
    ]
    modules["sglang.srt.entrypoints.http_server"].app = object()
    native_calls = []
    modules["sglang.srt.entrypoints.engine"].init_tokenizer_manager = lambda *a, **kw: (
        native_calls.append((a, kw)) or "native"
    )
    installed = []
    monkeypatch.setattr(
        server, "install_route", lambda app, channel: installed.append(channel.endpoint)
    )
    config = args()
    ports = SimpleNamespace(rpc_ipc_name="ipc:///private")
    assert (
        server.init_tokenizer_manager(server_args=config, port_args=ports) == "native"
    )
    assert installed == ["ipc:///private"]
    assert native_calls[0][1]["server_args"] is config


@pytest.mark.parametrize(
    "options",
    [
        ["--tp-size", "2"],
        ["--trust-remote-code"],
        ["--context-length", "0"],
        ["--context-length=4097"],
        ["--mem-fraction-static=nan"],
        ["--mem-fraction-static", "1"],
        ["--context-length"],
        ["--context-length", "256", "--context-length", "512"],
    ],
)
def test_only_bounded_unique_memory_context_tuning(options):
    with pytest.raises(ValueError):
        _adapter(engine_args=options)


def test_launch_uses_official_http_wrapper_and_fixed_profile(monkeypatch, tmp_path):
    (tmp_path / "config.json").write_text("{}")
    monkeypatch.setattr(adapter_module, "validate_config", lambda raw: None)
    monkeypatch.setattr(adapter_module, "check_runtime_sources", lambda kind: None)
    monkeypatch.setattr(adapter_module, "validate_environment", lambda: None)
    adapter = _adapter(engine_args=["--context-length=512"])
    command = adapter.launch_cmd(tmp_path, 8001)
    assert command[:3] == [
        sys.executable,
        "-m",
        "chalkcompute.weights.serving.sglang_resident_server",
    ]
    for flag in [
        "--disable-cuda-graph",
        "--disable-radix-cache",
        "--disable-overlap-schedule",
        "--enable-deterministic-inference",
    ]:
        assert flag in command
    assert "--context-length=512" in command
    assert "--context-length" not in command
    with pytest.raises(ValueError, match="port"):
        adapter.launch_cmd(tmp_path, 9000)


def test_unknown_sglang_environment_is_rejected(monkeypatch):
    monkeypatch.setenv("SGLANG_UNKNOWN_OVERRIDE", "1")
    with pytest.raises(ValueError):
        adapter_module.validate_environment()


def manifest_file(directory: Path, version=1, parent=None, kind="keyframe"):
    manifest = WeightManifest(
        version,
        parent,
        kind,
        "example/tiny",
        "a" * 64,
        (
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
        ),
    )
    (directory / "manifest.json").write_bytes(manifest.to_json())
    return manifest


def test_initialize_and_apply_ack_are_bound_to_identity_version_and_sha(tmp_path):
    initial = tmp_path / "initial"
    patch = tmp_path / "patch"
    initial.mkdir()
    patch.mkdir()
    first = manifest_file(initial)
    second = manifest_file(patch, 2, 1, "patch")
    adapter = _adapter()
    adapter._model_dir = initial.resolve()

    async def post(path, body):
        assert path == server.CONTROL_PATH
        return {
            "ok": True,
            "version": body["version"],
            "manifest_sha256": body["manifest_sha256"],
            "identity_sha256": "b" * 64,
            "profile": adapter_module.PROFILE,
            "layout_sha256": _LAYOUT.sha256,
            "schema_sha256": _LAYOUT.source_schema_sha256,
        }

    adapter._post = post
    asyncio.run(adapter.initialize(initial, first))
    asyncio.run(adapter.apply(patch, second))
    assert adapter._identity == "b" * 64


@pytest.mark.parametrize(
    "bad",
    [
        {"identity_sha256": "c" * 64},
        {"version": True},
        {"manifest_sha256": "c" * 64},
        {"profile": "other"},
        {"layout_sha256": "c" * 64},
        {"schema_sha256": "c" * 64},
        {"ok": False},
    ],
)
def test_uncertain_ack_is_sticky_restart_required(tmp_path, bad):
    manifest = manifest_file(tmp_path)
    adapter = _adapter()
    adapter._identity = "b" * 64
    ack = {
        "ok": True,
        "version": 1,
        "manifest_sha256": hashlib.sha256(manifest.to_json()).hexdigest(),
        "identity_sha256": "b" * 64,
        "profile": adapter_module.PROFILE,
        "layout_sha256": _LAYOUT.sha256,
        "schema_sha256": _LAYOUT.source_schema_sha256,
    }
    adapter._post = AsyncMock(return_value={**ack, **bad})
    with pytest.raises(RuntimeError):
        asyncio.run(adapter.apply(tmp_path, manifest))
    assert adapter._failed
    with pytest.raises(RuntimeError, match="restart"):
        asyncio.run(adapter.apply(tmp_path, manifest))
    assert adapter._post.await_count == 1
