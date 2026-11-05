from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from chalkcompute.weights.serving.adapters.vllm import VLLMAdapter
from chalkcompute.weights.serving.profiling import (
    COUNTS,
    HOST_PHASES,
    WorkerTimer,
    validate_profiles,
)


def profile(rank=0):
    return {
        "schema_version": 1,
        "version": 7,
        "rank": rank,
        "local_rank": rank,
        "worker_pid": 42 + rank,
        "device": f"cuda:{rank}",
        "host_ms": dict.fromkeys(HOST_PHASES, 0.0),
        "stream_elapsed_ms": {"h2d_host": 0.1},
        "counts": dict.fromkeys(COUNTS, 0),
    }


def test_profiles_require_each_global_rank_and_preserve_identity():
    results = [{"profile": profile(1)}, {"profile": profile(0)}]
    assert [p["rank"] for p in validate_profiles(results, 7, 2)] == [0, 1]
    with pytest.raises(ValueError, match="rank"):
        validate_profiles([results[0], results[0]], 7, 2)
    with pytest.raises(ValueError, match="Missing"):
        validate_profiles([results[0]], 7, 2)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, "1"])
def test_rejects_nonfinite_or_untyped_telemetry(value):
    p = profile()
    p["host_ms"]["decode"] = value
    with pytest.raises(ValueError, match="finite"):
        validate_profiles([{"profile": p}], 7, 1)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 8),
        ("rank", -1),
        ("worker_pid", 0),
        ("local_rank", True),
        ("device", "cuda:0\nsecret"),
    ],
)
def test_rejects_unexpected_version_or_identity(field, value):
    p = profile()
    p[field] = value
    with pytest.raises(ValueError):
        validate_profiles([{"profile": p}], 7, 1)


def test_rejects_unknown_phases_and_negative_counts():
    p = profile()
    p["stream_elapsed_ms"]["kernel_busy_ms"] = 1
    with pytest.raises(ValueError, match="phases"):
        validate_profiles([{"profile": p}], 7, 1)
    p = profile()
    p["counts"]["payload_bytes"] = -1
    with pytest.raises(ValueError, match="counts"):
        validate_profiles([{"profile": p}], 7, 1)


def test_cpu_timer_emits_finite_schema_without_cuda_access():
    device = SimpleNamespace(type="cpu")
    timer = WorkerTimer(True, None, device)
    with timer.phase("decode"):
        pass
    p = timer.result(SimpleNamespace(rank=0, local_rank=0), 7)
    p["device"] = "cpu"
    assert validate_profiles([{"profile": p}], 7, 1)[0]["host_ms"]["decode"] >= 0
    assert p["stream_elapsed_ms"] == {}


def test_cuda_timer_records_current_device_stream_without_synchronizing():
    calls = []

    class Context:
        def __enter__(self):
            calls.append("enter_device")

        def __exit__(self, *args):
            calls.append("exit_device")

    class Event:
        def record(self, stream):
            calls.append(("record", stream))

        def elapsed_time(self, other):
            calls.append("elapsed")
            return 2.5

    cuda = SimpleNamespace(
        device=lambda d: Context(),
        current_stream=lambda d: "worker-stream",
        Event=lambda **kw: Event(),
    )
    timer = WorkerTimer(True, SimpleNamespace(cuda=cuda), SimpleNamespace(type="cuda"))
    with timer.phase("h2d_host"):
        calls.append("copy")
    assert calls.count(("record", "worker-stream")) == 2
    # Caller performs its existing final synchronize before asking for result.
    calls.append("existing_final_synchronize")
    result = timer.result(SimpleNamespace(rank=0, local_rank=0), 7)
    assert result["stream_elapsed_ms"] == {"h2d_host": 2.5}
    assert calls.index("elapsed") > calls.index("existing_final_synchronize")


def test_disabled_timer_creates_no_events_or_timings():
    timer = WorkerTimer(False, None, SimpleNamespace(type="cuda"))
    with timer.phase("h2d_host"):
        pass
    assert not timer.events and not any(timer.host.values())


def test_adapter_validates_before_logging_and_telemetry_is_opt_in(tmp_path, caplog):
    class Adapter(VLLMAdapter):
        async def _post(self, path, body):
            self.body = body
            return {"results": [{"ok": True, "version": 7, "profile": profile()}]}

    adapter = Adapter(profile=True)
    caplog.set_level("INFO")
    asyncio.run(adapter.apply(tmp_path, SimpleNamespace(version=7)))
    assert adapter.body["args"][-1] is True
    assert adapter.last_profile["workers"][0]["rank"] == 0
    assert json.loads(caplog.records[-1].message)["phase"] == "apply"
    bad = copy.deepcopy(profile())
    bad["host_ms"]["decode"] = float("nan")

    class Bad(Adapter):
        async def _post(self, path, body):
            return {"results": [{"ok": True, "version": 7, "profile": bad}]}

    adapter = Bad(profile=True)
    with pytest.raises(ValueError):
        asyncio.run(adapter.apply(tmp_path, SimpleNamespace(version=7)))
    assert adapter.last_profile is None
    caplog.clear()
    adapter = Adapter()
    asyncio.run(adapter.apply(tmp_path, SimpleNamespace(version=7)))
    assert adapter.body["args"] == [str(tmp_path), "7"]
    assert adapter.last_profile is None and not caplog.records


@pytest.mark.parametrize("enabled", [False, True])
def test_worker_profiles_real_cpu_dense_and_sparse_decode(
    tmp_path, monkeypatch, enabled
):
    import importlib
    from chalkcompute._weights_codec import checksum, diff, encode_patch
    from chalkcompute._weights_format import TensorEntry, WeightManifest
    from chalkcompute.weights.serving.vllm_worker_ext import WeightSyncExtension

    torch = pytest.importorskip("torch")
    pytest.importorskip("zstandard")
    entries = []
    payloads = []
    for name, count in [("dense", 100), ("sparse", 1)]:
        old = torch.zeros(100)
        new = old.clone()
        new[:count] = 1
        patch = diff(old, new, name=name)
        data = encode_patch(patch)
        entries.append(
            TensorEntry(
                name,
                patch.dtype,
                patch.shape,
                patch.encoding,
                "patches.bin",
                sum(map(len, payloads)),
                len(data),
                patch.nnz,
                checksum(data),
            )
        )
        payloads.append(data)
    (tmp_path / "patches.bin").write_bytes(b"".join(payloads))
    manifest = WeightManifest(7, 6, "patch", "test", "a" * 64, tuple(entries))
    (tmp_path / "manifest.json").write_bytes(manifest.to_json())
    loaded = {}
    sparse = []

    def load_weights(*, weights):
        loaded.update({name: value.clone() for name, value in weights})

    native = SimpleNamespace(
        CheckpointWeightPatch=lambda name, shape, dtype, values, indices: (
            SimpleNamespace(name=name, values=values, indices=indices)
        ),
        load_checkpoint_weight_patches=lambda model, patches, **kw: sparse.extend(
            patches
        ),
    )
    original = importlib.import_module
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: (
            native
            if name == "vllm.model_executor.model_loader.checkpoint_weight_patch"
            else original(name)
        ),
    )
    worker = WeightSyncExtension()
    worker.device = torch.device("cpu")
    worker.model_runner = SimpleNamespace(
        get_model=lambda: SimpleNamespace(load_weights=load_weights)
    )
    if enabled:
        worker.rank, worker.local_rank = 0, 0
    result = worker.ws_apply_dir(str(tmp_path), 7, enabled)
    assert torch.equal(loaded["dense"], torch.ones(100))
    assert len(sparse) == 1 and sparse[0].indices.tolist() == [0]
    if enabled:
        p = validate_profiles([result], 7, 1)[0]
        assert p["counts"] == {
            "payload_bytes": sum(map(len, payloads)),
            "value_bytes": 404,
            "index_bytes": 8,
            "tensors": 2,
            "sparse_batches": 1,
        }
        assert p["stream_elapsed_ms"] == {}
    else:
        assert result == {"ok": True, "version": 7}
