from __future__ import annotations

import dataclasses
import importlib.metadata
import sys
import subprocess
from types import ModuleType, SimpleNamespace

import pytest

from chalkcompute._weights_codec import TensorPatch, checksum, encode_patch
from chalkcompute._weights_format import TensorEntry, WeightManifest
from chalkcompute.weights.serving import vllm_resident_sparse as sparse

torch = pytest.importorskip("torch")


@pytest.fixture
def workers(monkeypatch):
    monkeypatch.setattr(sparse, "HIDDEN", 4)
    monkeypatch.setattr(sparse, "VOCAB", 16)
    monkeypatch.setattr(torch.cuda, "synchronize", lambda _: None)
    result = [
        SimpleNamespace(
            rank=rank,
            device=torch.device("cpu"),
            targets={
                sparse.NORM: torch.nn.Parameter(torch.ones(4, dtype=torch.bfloat16)),
                sparse.HEAD: torch.nn.Parameter(
                    torch.ones((2, 4), dtype=torch.bfloat16)
                ),
            },
        )
        for rank in range(8)
    ]

    def snapshot(worker):
        identity = {
            "rank": worker.rank,
            "tp_rank": worker.rank,
            "identity_sha256": str(worker.rank) * 64,
            "targets": {
                name: {"data_ptr": parameter.data_ptr()}
                for name, parameter in worker.targets.items()
            },
        }
        return worker.targets, identity

    monkeypatch.setattr(sparse, "_snapshot", snapshot)
    return result


def write_patch(
    root, *, version=1, parent=0, nan=False, dense=False, unsupported=False
):
    root.mkdir()
    entries = []
    for name, shape, indices in (
        (sparse.NORM, (4,), [0, 3]),
        (sparse.HEAD, (16, 4), [rank * 8 + col for rank in range(8) for col in (0, 7)]),
    ):
        values = torch.full(
            (len(indices),), float("nan") if nan else 2, dtype=torch.bfloat16
        )
        patch = TensorPatch(name, shape, "bfloat16", values, torch.tensor(indices))
        data = encode_patch(patch)
        filename = f"{len(entries)}.bin"
        (root / filename).write_bytes(data)
        entries.append(
            TensorEntry(
                name,
                "bfloat16",
                shape,
                "sparse",
                filename,
                0,
                len(data),
                len(indices),
                checksum(data),
            )
        )
    if dense:
        entries[0] = dataclasses.replace(entries[0], encoding="dense", nnz=4)
    if unsupported:
        entries[1] = dataclasses.replace(entries[1], name="packed.experts.weight")
    manifest = WeightManifest(
        version, parent, "patch", "moonshotai/Kimi-K3", "a" * 64, tuple(entries)
    )
    (root / "manifest.json").write_bytes(manifest.to_json())
    return root


def prepared(workers, root, transaction="a" * 32, version=1):
    acknowledgements = [
        sparse.prepare(worker, str(root), version, transaction) for worker in workers
    ]
    identities = {str(ack["rank"]): ack["identity_sha256"] for ack in acknowledgements}
    assert len({ack["manifest_sha256"] for ack in acknowledgements}) == 1
    return identities, acknowledgements[0]["manifest_sha256"]


def test_all_rank_mapping_and_only_indexed_resident_writes(workers, tmp_path):
    from torch.utils._python_dispatch import TorchDispatchMode

    root = write_patch(tmp_path / "patch")
    before = [
        {name: value.detach().clone() for name, value in worker.targets.items()}
        for worker in workers
    ]
    pointers = [
        {name: value.data_ptr() for name, value in worker.targets.items()}
        for worker in workers
    ]
    identities, manifest = prepared(workers, root)
    for worker in workers:
        assert all(
            torch.equal(worker.targets[name], value)
            for name, value in before[worker.rank].items()
        )
    operations = []

    class ScatterOnly(TorchDispatchMode):
        def __torch_dispatch__(self, func, types, args=(), kwargs=None):
            operations.append(str(func))
            assert str(func) in ("aten.view.default", "aten.index_copy_.default")
            return func(*args, **(kwargs or {}))

    with ScatterOnly():
        acks = [
            sparse.commit(worker, "a" * 32, 1, identities, manifest)
            for worker in workers
        ]
    assert operations.count("aten.index_copy_.default") == 16
    for worker, ack in zip(workers, acks):
        assert ack["committed"] and ack["local_nnz"] == {sparse.NORM: 2, sparse.HEAD: 2}
        assert worker._ws_resident_version == 1
        assert worker.targets[sparse.NORM].tolist() == [2, 1, 1, 2]
        assert worker.targets[sparse.HEAD].flatten().tolist() == [
            2,
            1,
            1,
            1,
            1,
            1,
            1,
            2,
        ]
        assert {
            name: value.data_ptr() for name, value in worker.targets.items()
        } == pointers[worker.rank]


@pytest.mark.parametrize(
    "change", ["dense", "unsupported", "nan", "parent", "oversized"]
)
def test_invalid_preparation_never_changes_any_resident_parameter(
    workers, tmp_path, change
):
    root = write_patch(
        tmp_path / "patch",
        dense=change == "dense",
        unsupported=change == "unsupported",
        nan=change == "nan",
        parent=9 if change == "parent" else 0,
        version=10 if change == "parent" else 1,
    )
    if change == "oversized":
        (root / "0.bin").write_bytes(b"x" * (sparse.MAX_PAYLOAD + 1))
    worker = workers[0]
    with pytest.raises((ValueError, RuntimeError)):
        sparse.prepare(worker, str(root), 10 if change == "parent" else 1, "a" * 32)
    assert not hasattr(worker, "_ws_resident_pending")
    assert all((value == 1).all().item() for value in worker.targets.values())


def test_all_rank_prevalidation_then_idempotent_commit_and_next_version(
    workers, tmp_path, monkeypatch
):
    root = write_patch(tmp_path / "patch")
    identities, manifest = prepared(workers, root)
    worker = workers[0]
    again = sparse.prepare(worker, str(root), 1, "a" * 32)
    assert again["manifest_sha256"] == manifest
    with pytest.raises(ValueError, match="all eight ranks"):
        sparse.commit(worker, "a" * 32, 1, {"0": identities["0"]}, manifest)
    assert all((value == 1).all().item() for value in worker.targets.values())
    ack = sparse.commit(worker, "a" * 32, 1, identities, manifest)
    monkeypatch.setattr(
        torch.Tensor,
        "index_copy_",
        lambda *args, **kwargs: pytest.fail("replay wrote memory"),
    )
    assert sparse.commit(worker, "a" * 32, 1, identities, manifest) == ack
    with pytest.raises(ValueError, match="replay differs"):
        sparse.commit(worker, "a" * 32, 2, identities, manifest)
    next_root = write_patch(tmp_path / "next", version=2, parent=1)
    assert sparse.prepare(worker, str(next_root), 2, "b" * 32)["parent_version"] == 1


def test_changed_worker_identity_aborts_before_write(workers, tmp_path, monkeypatch):
    identities, manifest = prepared(workers, write_patch(tmp_path / "patch"))
    worker = workers[0]
    old = sparse._snapshot

    def changed(value):
        targets, identity = old(value)
        return targets, {**identity, "identity_sha256": "f" * 64}

    monkeypatch.setattr(sparse, "_snapshot", changed)
    with pytest.raises(ValueError):
        sparse.commit(worker, "a" * 32, 1, identities, manifest)
    assert all((value == 1).all().item() for value in worker.targets.values())


def test_uncertain_mutation_stays_failed_and_abort_does_not_clear(
    workers, tmp_path, monkeypatch
):
    identities, manifest = prepared(workers, write_patch(tmp_path / "patch"))
    worker = workers[0]
    monkeypatch.setattr(
        torch.cuda,
        "synchronize",
        lambda _: (_ for _ in ()).throw(RuntimeError("lost GPU")),
    )
    with pytest.raises(RuntimeError, match="lost GPU"):
        sparse.commit(worker, "a" * 32, 1, identities, manifest)
    sparse.abort(worker, "a" * 32)
    with pytest.raises(RuntimeError, match="restart"):
        sparse.prepare(worker, str(tmp_path / "patch"), 1, "b" * 32)


def test_abort_requires_matching_transaction_and_releases_staging(workers, tmp_path):
    prepared(workers, write_patch(tmp_path / "patch"))
    worker = workers[0]
    with pytest.raises(ValueError, match="different"):
        sparse.abort(worker, "b" * 32)
    assert sparse.abort(worker, "a" * 32)["aborted"]
    assert worker._ws_resident_pending is None
    assert all((value == 1).all().item() for value in worker.targets.values())


def test_source_coordinate_boundaries_map_to_exactly_one_rank():
    indices = torch.tensor(
        [
            rank * (sparse.VOCAB // 8) * sparse.HIDDEN + col
            for rank in range(8)
            for col in (0, (sparse.VOCAB // 8) * sparse.HIDDEN - 1)
        ]
    )
    coverage = torch.zeros(len(indices), dtype=torch.int64)
    for rank in range(8):
        mapped, selected = sparse.local_indices(sparse.HEAD, indices, rank)
        coverage += selected
        assert mapped.tolist() == [0, (sparse.VOCAB // 8) * sparse.HIDDEN - 1]
    assert coverage.tolist() == [1] * 16
    with pytest.raises(ValueError):
        sparse.local_indices("packed.experts", indices, 0)


def install_module(monkeypatch, name, **attributes):
    module = ModuleType(name)
    module.__dict__.update(attributes)
    monkeypatch.setitem(sys.modules, name, module)
    return module


@pytest.mark.parametrize("runtime_version", ["0.33.0", "0.33.0+rocm100"])
@pytest.mark.parametrize(
    "change",
    [None, "version", "topology", "rank", "model", "config", "device", "architecture"],
)
def test_native_context_rejects_unsupported_runtime_before_any_tensor_access(
    monkeypatch, change, runtime_version
):
    world = SimpleNamespace(world_size=8, rank=0)
    tp = SimpleNamespace(world_size=8, rank_in_group=0)
    parallel = SimpleNamespace(
        get_world_group=lambda: world,
        get_tp_group=lambda: tp,
        get_pp_group=lambda: SimpleNamespace(world_size=1),
        get_dp_group=lambda: SimpleNamespace(world_size=1),
    )
    install_module(monkeypatch, "vllm")
    install_module(monkeypatch, "vllm.distributed", parallel_state=parallel)
    monkeypatch.setitem(sys.modules, "vllm.distributed.parallel_state", parallel)
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda _: "0.32.0" if change == "version" else runtime_version,
    )
    monkeypatch.setattr(torch.version, "hip", "10.0")
    monkeypatch.setattr(
        torch.cuda,
        "get_device_properties",
        lambda _: SimpleNamespace(
            gcnArchName="gfx942"
            if change == "architecture"
            else "gfx950:sramecc+:xnack-"
        ),
    )
    monkeypatch.setattr(
        sparse,
        "Path",
        lambda _: SimpleNamespace(read_text=lambda: "1 (worker) " + "0 " * 19 + "123"),
    )
    model_type = type(
        "KimiK3ForConditionalGeneration",
        (),
        {"__module__": "vllm.models.kimi_k3.amd.model"},
    )
    model = model_type()
    model.config = SimpleNamespace(
        model_type="kimi_k3",
        text_config=SimpleNamespace(hidden_size=7168, vocab_size=163840),
    )
    worker = SimpleNamespace(
        device=torch.device("cuda:0"),
        model_runner=SimpleNamespace(get_model=lambda: model),
    )
    if change == "topology":
        tp.world_size = 4
    elif change == "rank":
        tp.rank_in_group = 1
    elif change == "model":
        model_type.__module__ = "unreviewed.model"
    elif change == "config":
        model.config.text_config.hidden_size = 4096
    elif change == "device":
        worker.device = torch.device("cpu")
    if change is None:
        actual, identity = sparse._context(worker)
        assert (
            actual is model and identity["rank"] == 0 and identity["tp_world_size"] == 8
        )
    else:
        with pytest.raises(ValueError):
            sparse._context(worker)


@pytest.mark.parametrize(
    "runtime_version",
    [
        "0.32.0",
        "0.34.0",
        "0.33.0.dev1+rocm100",
        "0.33.0.post1+rocm100",
        "0.33.0+rocm72",
        "0.33.0+rocm100.extra",
        "0.33.0+cu130",
        "0.33.0+cpu",
    ],
)
def test_unreviewed_version_rejected_before_model_access(monkeypatch, runtime_version):
    install_module(monkeypatch, "vllm")
    install_module(monkeypatch, "vllm.distributed")
    install_module(monkeypatch, "vllm.distributed.parallel_state")
    monkeypatch.setattr(importlib.metadata, "version", lambda _: runtime_version)
    worker = SimpleNamespace(
        model_runner=SimpleNamespace(
            get_model=lambda: pytest.fail("Unreviewed version accessed resident model")
        )
    )
    with pytest.raises(ValueError, match="observed"):
        sparse._context(worker)


@pytest.mark.parametrize(
    "change",
    [
        None,
        "dtype",
        "device",
        "noncontiguous",
        "offset",
        "storage",
        "quantized",
        "padding",
        "shape",
        "alias",
    ],
)
def test_native_target_storage_and_sharding_gates(monkeypatch, change):
    class Parameter:
        def __init__(self, shape, pointer):
            self.shape, self.pointer = shape, pointer
            self.dtype, self.device = torch.bfloat16, torch.device("cuda:0")
            self.contiguous, self.offset, self.extra = True, 0, 0

        def numel(self):
            import math

            return math.prod(self.shape)

        def element_size(self):
            return 2

        def is_contiguous(self):
            return self.contiguous

        def storage_offset(self):
            return self.offset

        def untyped_storage(self):
            return SimpleNamespace(
                data_ptr=lambda: self.pointer,
                nbytes=lambda: self.numel() * 2 + self.extra,
            )

    class Norm:
        pass

    class Head:
        pass

    class Unquantized:
        pass

    install_module(monkeypatch, "vllm.model_executor.layers.layernorm", RMSNorm=Norm)
    install_module(
        monkeypatch,
        "vllm.model_executor.layers.vocab_parallel_embedding",
        ParallelLMHead=Head,
        UnquantizedEmbeddingMethod=Unquantized,
    )
    monkeypatch.setattr(torch.nn, "Parameter", Parameter)
    norm, head = Norm(), Head()
    norm.weight = Parameter((7168,), 1)
    head.weight = Parameter((20480, 7168), 2)
    head.quant_method = Unquantized()
    head.tp_rank, head.tp_size = 3, 8
    head.num_embeddings = head.org_vocab_size = head.num_embeddings_padded = 163840
    head.num_added_embeddings = 0
    head.shard_indices = SimpleNamespace(
        org_vocab_start_index=61440,
        org_vocab_end_index=81920,
        padded_org_vocab_start_index=61440,
        padded_org_vocab_end_index=81920,
        num_added_elements=0,
        num_added_elements_padded=0,
    )
    if change == "dtype":
        norm.weight.dtype = torch.float32
    elif change == "device":
        norm.weight.device = torch.device("cuda:1")
    elif change == "noncontiguous":
        norm.weight.contiguous = False
    elif change == "offset":
        norm.weight.offset = 1
    elif change == "storage":
        norm.weight.extra = 4
    elif change == "quantized":
        head.quant_method = object()
    elif change == "padding":
        head.shard_indices.padded_org_vocab_end_index += 1
    elif change == "shape":
        head.weight.shape = (20481, 7168)
    elif change == "alias":
        norm.weight.pointer = head.weight.pointer
    model = SimpleNamespace(
        language_model=SimpleNamespace(model=SimpleNamespace(norm=norm), lm_head=head)
    )
    if change is None:
        assert set(sparse._targets(model, {"tp_rank": 3, "device": "cuda:0"})) == {
            sparse.NORM,
            sparse.HEAD,
        }
    else:
        with pytest.raises(ValueError):
            sparse._targets(model, {"tp_rank": 3, "device": "cuda:0"})


def test_real_serving_package_import_needs_no_volume_native_or_accelerator_dependencies():
    program = """
import importlib.abc
import sys
class RefuseOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"chalkcompute._weights", "chalkcompute._volume", "torch", "vllm"}:
            raise AssertionError("unexpected optional import: " + fullname)
sys.meta_path.insert(0, RefuseOptional())
import chalkcompute.weights.serving.vllm_worker_ext
import chalkcompute.weights.serving.vllm_resident_sparse
import chalkcompute._weights_codec
import chalkcompute.weights
assert "chalkcompute._weights" not in sys.modules
assert chalkcompute.weights.WeightStreamError.__name__ == "WeightStreamError"
"""
    subprocess.run(
        [sys.executable, "-B", "-c", program],
        check=True,
        capture_output=True,
        timeout=15,
    )


def test_lazy_weights_exports_preserve_public_classes():
    from chalkcompute import _weights
    from chalkcompute import weights

    assert weights.WeightPublisher is _weights.WeightPublisher
    assert weights.WeightStream is _weights.WeightStream
    with pytest.raises(AttributeError):
        getattr(weights, "does_not_exist")
