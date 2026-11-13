"""Real tiny non-model-specific layout/codec/core exercised through both bridges.

CPU seams replace CUDA admission/synchronization only; this is not a GPU claim.
"""

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from chalkcompute._weights_codec import TensorPatch, checksum, encode_patch
from chalkcompute._weights_format import (
    TensorEntry,
    WeightManifest,
    WeightStreamError,
    architecture_hash,
)
from chalkcompute.weights.layout import (
    Concatenate,
    TensorMapping,
    WeightLayout,
    WeightSchema,
)
from chalkcompute.weights.serving import resident, resident_runtime
from chalkcompute.weights.serving.adapters.resident_control import LayoutSnapshot

torch = pytest.importorskip("torch")


def snapshot(self):
    return {
        "model": id(self.model),
        "params": {
            n: (id(p), p.data_ptr(), tuple(p.shape))
            for n, p in self.model.named_parameters(remove_duplicate=False)
        },
    }


def fixture(tmp_path):
    from safetensors.torch import save_file

    initial_dir = tmp_path / "initial"
    initial_dir.mkdir()
    config = b'{"model_type":"custom_encoder","dtype":"bfloat16"}'
    logical = {
        "encoder.left": torch.arange(128).to(torch.bfloat16),
        "encoder.right": torch.arange(128, 256).to(torch.bfloat16),
    }
    checkpoint = initial_dir / "model.safetensors"
    save_file(logical, str(checkpoint))
    payload = checkpoint.read_bytes()
    tensors = tuple(
        TensorEntry(
            n,
            "bfloat16",
            (128,),
            "dense",
            checkpoint.name,
            0,
            len(payload),
            128,
            checksum(payload),
        )
        for n in logical
    )
    arch = architecture_hash(
        config,
        (
            (n, SimpleNamespace(shape=t.shape, dtype="bfloat16"))
            for n, t in logical.items()
        ),
    )
    initial = WeightManifest(0, None, "keyframe", "customer/encoder", arch, tensors)
    (initial_dir / "manifest.json").write_bytes(initial.to_json())
    (initial_dir / "config.json").write_bytes(config)
    layout = WeightLayout(
        (
            TensorMapping(
                ("encoder.left", "encoder.right"), "packed", (Concatenate(0),)
            ),
        ),
        False,
    ).bind(WeightSchema.from_manifest(initial))
    # Whitespace is allowed; transport raw SHA differs from canonical layout SHA.
    raw = json.dumps(json.loads(layout.to_json()), indent=2).encode()
    layout_file = tmp_path / "caller-layout.json"
    layout_file.write_bytes(raw)
    model = torch.nn.Module()
    model.register_parameter(
        "packed", torch.nn.Parameter(torch.cat(list(logical.values())))
    )
    patch_dir = tmp_path / "patch"
    patch_dir.mkdir()
    encoded = encode_patch(
        TensorPatch(
            "encoder.right",
            (128,),
            "bfloat16",
            torch.tensor([999], dtype=torch.bfloat16),
            torch.tensor([7]),
        )
    )
    (patch_dir / "payload.bin").write_bytes(encoded)
    patch = WeightManifest(
        1,
        0,
        "patch",
        initial.base_model,
        arch,
        (
            TensorEntry(
                "encoder.right",
                "bfloat16",
                (128,),
                "sparse",
                "payload.bin",
                0,
                len(encoded),
                1,
                checksum(encoded),
            ),
        ),
    )
    (patch_dir / "manifest.json").write_bytes(patch.to_json())
    return initial_dir, initial, patch_dir, patch, layout_file, raw, model


@pytest.mark.parametrize("engine", ["vllm", "sglang"])
@pytest.mark.parametrize(
    "seed", ["stream", "snapshot", "wrong_snapshot", "wrong_source"]
)
def test_bridge_exact_layout_handoff_and_real_generic_sparse_update(
    tmp_path, monkeypatch, engine, seed
):
    initial_dir, initial, patch_dir, patch, path, raw, model = fixture(tmp_path)
    monkeypatch.setattr(resident.ResidentWeights, "_snapshot", snapshot)
    monkeypatch.setattr(resident.ResidentWeights, "_synchronize", lambda self: None)
    monkeypatch.setattr(resident_runtime, "check_runtime_sources", lambda kind: None)
    monkeypatch.setattr(resident_runtime, "check_cuda", lambda device: None)
    launch_dir = initial_dir if seed == "stream" else tmp_path / "snapshot"
    launch_dir.mkdir(exist_ok=True)
    expected_source = initial_dir if seed == "wrong_source" else launch_dir
    if seed == "wrong_snapshot":
        with torch.no_grad():
            model.packed[0] += 1
    if engine == "vllm":
        from chalkcompute.weights.serving.adapters.vllm_resident import (
            VLLMResidentAdapter,
        )
        from chalkcompute.weights.serving import vllm_resident_worker as worker

        adapter = VLLMResidentAdapter(
            weight_layout=path, weight_layout_sha256=hashlib.sha256(raw).hexdigest()
        )
        native = worker.ResidentSparseExtension()
        native.vllm_config = SimpleNamespace(
            model_config=SimpleNamespace(model=str(expected_source))
        )
        monkeypatch.setattr(worker, "check_worker", lambda _: model)

        async def post(route, body):
            return {"results": [getattr(native, body["method"])(*body["args"])]}
    else:
        from chalkcompute.weights.serving.adapters.sglang_resident import (
            SGLangResidentAdapter,
        )
        from chalkcompute.weights.serving import sglang_resident_worker as worker

        adapter = SGLangResidentAdapter(
            weight_layout=path, weight_layout_sha256=hashlib.sha256(raw).hexdigest()
        )
        native = SimpleNamespace(
            server_args=SimpleNamespace(model_path=str(expected_source)),
            tp_worker=SimpleNamespace(model_runner=SimpleNamespace(model=model)),
            flush_cache=lambda: True,
        )
        monkeypatch.setattr(worker, "check_scheduler", lambda _: None)

        async def post(route, body):
            return worker.control(native, body)

    adapter._model_dir = launch_dir.resolve()
    adapter._post = post
    path.write_text("corrupted after snapshot")
    before = model.packed.detach().clone()
    identity = (id(model.packed), model.packed.data_ptr())

    async def run():
        if seed.startswith("wrong"):
            with pytest.raises((ValueError, RuntimeError, WeightStreamError)):
                await adapter.initialize(initial_dir, initial)
            assert adapter._failed
            assert getattr(native, "_chalk_resident", None) is None
            return
        await adapter.initialize(initial_dir, initial)
        await adapter.apply(patch_dir, patch)
        assert (id(model.packed), model.packed.data_ptr()) == identity
        expected = before.clone()
        expected[128 + 7] = 999
        assert torch.equal(model.packed, expected)
        # Exact receipt replay is idempotent and must not repeat resident writes.
        native._chalk_resident._write = lambda *args: pytest.fail(
            "Replay wrote weights"
        )
        await adapter.apply(patch_dir, patch)
        # Reusing the version with a different manifest fails closed.
        from dataclasses import replace

        conflict = replace(patch, trainer_step=17)
        (patch_dir / "manifest.json").write_bytes(conflict.to_json())
        with pytest.raises(Exception):
            await adapter.apply(patch_dir, conflict)
        assert adapter._failed

    asyncio.run(run())


@pytest.mark.parametrize("mutation", ["hash", "invalid_json", "unbound", "symlink"])
def test_layout_file_refuses_invalid_pins_before_engine_launch(tmp_path, mutation):
    *_, path, raw, _model = fixture(tmp_path)
    if mutation == "invalid_json":
        raw = b"{}"
        path.write_bytes(raw)
    elif mutation == "unbound":
        raw = WeightLayout().to_json()
        path.write_bytes(raw)
    elif mutation == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(path)
        path = link
    digest = "0" * 64 if mutation == "hash" else hashlib.sha256(raw).hexdigest()
    with pytest.raises((ValueError, OSError)):
        LayoutSnapshot(path, digest)


def test_layout_schema_binding_refuses_different_initial_keyframe(tmp_path):
    from dataclasses import replace

    initial_dir, initial, _p, _m, path, raw, _model = fixture(tmp_path)
    pinned = LayoutSnapshot(path, hashlib.sha256(raw).hexdigest())
    changed = replace(
        initial, tensors=(replace(initial.tensors[0], shape=(256,), nnz=256),)
    )
    with pytest.raises(ValueError, match="schema"):
        pinned.check_manifest(changed)
