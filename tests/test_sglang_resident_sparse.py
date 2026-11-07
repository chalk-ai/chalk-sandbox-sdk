from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from chalkcompute._weights_codec import TensorPatch, checksum, encode_patch
from chalkcompute._weights_format import TensorEntry, WeightManifest
from chalkcompute.weights.serving import sglang_resident_sparse as sparse

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
        version,
        parent,
        "patch",
        "deepseek-ai/DeepSeek-V4-Flash-0731",
        "a" * 64,
        tuple(entries),
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
    import sys
    from types import ModuleType

    module = ModuleType(name)
    module.__dict__.update(attributes)
    monkeypatch.setitem(sys.modules, name, module)
    return module


@pytest.mark.parametrize(
    "change",
    [
        None,
        "model",
        "hidden",
        "vocab",
        "tie",
        "hip",
        "device",
        "gfx",
        "tp",
        "pp",
        "dp",
        "cp",
        "rank",
        "head_group",
    ],
)
def test_native_context_requires_exact_model_device_and_topology(monkeypatch, change):
    parallel = SimpleNamespace(
        tp_size=8,
        pp_size=1,
        dp_size=1,
        attn_tp_size=8,
        attn_dp_size=1,
        attn_cp_size=1,
        enable_dp_lm_head=False,
        tp_rank=0,
    )
    world = SimpleNamespace(world_size=8, rank=0)
    install_module(
        monkeypatch, "sglang.srt.runtime_context", get_parallel=lambda: parallel
    )
    install_module(
        monkeypatch,
        "sglang.srt.distributed.parallel_state",
        get_world_group=lambda: world,
    )
    cls = type(
        "DeepseekV4ForCausalLM", (), {"__module__": "sglang.srt.models.deepseek_v4"}
    )
    model = cls()
    model.config = SimpleNamespace(
        model_type="deepseek_v4",
        hidden_size=4096,
        vocab_size=129280,
        tie_word_embeddings=False,
    )
    worker = SimpleNamespace(
        model_runner=SimpleNamespace(model=model), device="cuda", gpu_id=0
    )
    monkeypatch.setattr(torch.version, "hip", None if change == "hip" else "7.2")
    monkeypatch.setattr(
        torch.cuda,
        "get_device_properties",
        lambda _: SimpleNamespace(
            gcnArchName="gfx942" if change == "gfx" else "gfx950:xnack-"
        ),
    )
    monkeypatch.setattr(
        sparse,
        "Path",
        lambda _: SimpleNamespace(read_text=lambda: "1 (worker) " + "0 " * 19 + "123"),
    )
    if change == "model":
        cls.__module__ = "other.model"
    elif change == "hidden":
        model.config.hidden_size = 4
    elif change == "vocab":
        model.config.vocab_size = 16
    elif change == "tie":
        model.config.tie_word_embeddings = True
    elif change == "device":
        worker.device = "cpu"
    elif change == "tp":
        parallel.tp_size = 4
    elif change == "pp":
        parallel.pp_size = 2
    elif change == "dp":
        parallel.dp_size = 2
    elif change == "cp":
        parallel.attn_cp_size = 2
    elif change == "rank":
        parallel.tp_rank = 1
    elif change == "head_group":
        parallel.enable_dp_lm_head = True
    if change is None:
        actual, identity = sparse._context(worker)
        assert actual is model and identity["rank"] == 0
    else:
        with pytest.raises(ValueError):
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

    install_module(monkeypatch, "sglang.srt.layers.layernorm", RMSNorm=Norm)
    install_module(
        monkeypatch,
        "sglang.srt.layers.vocab_parallel_embedding",
        ParallelLMHead=Head,
        UnquantizedEmbeddingMethod=Unquantized,
    )
    monkeypatch.setattr(torch.nn, "Parameter", Parameter)
    norm, head = Norm(), Head()
    norm.weight = Parameter((4096,), 1)
    head.weight = Parameter((16160, 4096), 2)
    head.quant_method = Unquantized()
    head.enable_tp, head.use_attn_tp_group, head.tp_size = True, False, 8
    head.num_embeddings = head.org_vocab_size = head.num_embeddings_padded = 129280
    head.num_added_embeddings = 0
    head.shard_indices = SimpleNamespace(
        org_vocab_start_index=48480,
        org_vocab_end_index=64640,
        padded_org_vocab_start_index=48480,
        padded_org_vocab_end_index=64640,
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
        head.weight.shape = (16161, 4096)
    elif change == "alias":
        norm.weight.pointer = head.weight.pointer
    model = SimpleNamespace(model=SimpleNamespace(norm=norm), lm_head=head)
    if change is None:
        assert set(sparse._targets(model, {"tp_rank": 3, "device": "cuda:0"})) == {
            sparse.NORM,
            sparse.HEAD,
        }
    else:
        with pytest.raises(ValueError):
            sparse._targets(model, {"tp_rank": 3, "device": "cuda:0"})
