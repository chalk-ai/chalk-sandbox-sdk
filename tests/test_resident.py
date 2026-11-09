"""Core policy and real CPU codec/indexed-write tests; no runtime/GPU claims."""

from dataclasses import replace
import hashlib
import math
import subprocess
import sys
from types import SimpleNamespace

import pytest

from chalkcompute._weights_codec import TensorPatch, checksum, encode_patch
from chalkcompute._weights_format import (
    TensorEntry,
    WeightManifest,
    WeightStreamError,
    architecture_hash,
)
from chalkcompute.weights.serving import resident as core
from chalkcompute.weights.layout import (
    WeightLayout,
    WeightSchema,
    TensorMapping,
    Concatenate,
)

torch = pytest.importorskip("torch")
pytest.importorskip("zstandard")
pytest.importorskip("xxhash")


MODEL_ID = "customer/custom-encoder"
LOGICAL_SCHEMA = {"encoder.weight": (128,), "encoder.bias": (128,)}
NATIVE_SCHEMA = dict(LOGICAL_SCHEMA)


def config_bytes():
    return b'{"model_type":"customer_encoder","dtype":"bfloat16"}'


def bound_layout(initial):
    return WeightLayout().bind(WeightSchema.from_manifest(initial))


def make_resident(model, config, initial, layout=None):
    return core.ResidentWeights(model, config, initial, layout or bound_layout(initial))


def initial_manifest(config=None):
    config = config or config_bytes()
    schema = LOGICAL_SCHEMA
    return WeightManifest(
        0,
        None,
        "keyframe",
        MODEL_ID,
        architecture_hash(
            config,
            (
                (name, SimpleNamespace(shape=shape, dtype="bfloat16"))
                for name, shape in schema.items()
            ),
        ),
        tuple(
            TensorEntry(
                name,
                "bfloat16",
                shape,
                "dense",
                "initial.bin",
                0,
                1,
                math.prod(shape),
                "0" * 32,
            )
            for name, shape in schema.items()
        ),
    )


class TinyModel:
    def __init__(self):
        self.weight = torch.nn.Parameter(torch.zeros(128, dtype=torch.bfloat16))
        self.bias = torch.nn.Parameter(torch.zeros(128, dtype=torch.bfloat16))

    def named_parameters(self, **kwargs):
        return [("encoder.weight", self.weight), ("encoder.bias", self.bias)]

    def load_weights(self, *args, **kwargs):
        raise AssertionError("A resident update must not call a model loader")


def cpu_snapshot(self):
    # Private test seam only: production _snapshot requires the complete single-CUDA tensor catalog.
    return {
        "model_id": id(self.model),
        "parameters": {
            name: (
                id(value),
                value.data_ptr(),
                value.untyped_storage().data_ptr(),
                tuple(value.shape),
            )
            for name, value in self.model.named_parameters()
        },
    }


@pytest.fixture
def resident(monkeypatch):
    monkeypatch.setattr(core.ResidentWeights, "_snapshot", cpu_snapshot)
    monkeypatch.setattr(core.ResidentWeights, "_synchronize", lambda self: None)
    resident = make_resident(TinyModel(), config_bytes(), initial_manifest())
    # The actual base audit is tested separately below with CPU safetensors.
    # These codec tests isolate application policy through private test seams.
    resident._initial_verified = True
    return resident


def write_patch(root, initial, *, version=1, parent=0, updates=None, kind="patch"):
    root.mkdir()
    entries = []
    payload = bytearray()
    for name, indices, values in updates or []:
        patch = TensorPatch(
            name,
            next(entry.shape for entry in initial.tensors if entry.name == name),
            "bfloat16",
            torch.tensor(values, dtype=torch.bfloat16),
            torch.tensor(indices, dtype=torch.int64) if indices is not None else None,
        )
        encoded = encode_patch(patch)
        entries.append(
            TensorEntry(
                name,
                "bfloat16",
                patch.shape,
                patch.encoding,
                "payload.bin",
                len(payload),
                len(encoded),
                patch.nnz,
                checksum(encoded),
            )
        )
        payload.extend(encoded)
    if entries:
        (root / "payload.bin").write_bytes(payload)
    manifest = WeightManifest(
        version,
        parent,
        kind,
        initial.base_model,
        initial.architecture_hash,
        tuple(entries),
    )
    return save_manifest(root, manifest), manifest


def save_manifest(root, manifest):
    data = manifest.to_json()
    (root / "manifest.json").write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def test_pure_imports_do_not_load_torch_clients_or_engines():
    script = """
import sys
import chalkcompute.weights
from chalkcompute.weights.serving.resident import validate_config, load_manifest
assert not any(name in sys.modules for name in ['torch', 'chalkcompute._weights', 'chalkcompute._volume', 'vllm', 'sglang'])
from chalkcompute.weights import WeightStreamError
assert WeightStreamError.__name__ == 'WeightStreamError'
assert 'chalkcompute._volume' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", script], check=True, timeout=15)


@pytest.mark.parametrize("config", [b"[]", b'{"a":1,"a":2}', b'{"a":NaN}', b"not JSON"])
def test_config_refuses_invalid_bounded_json(config):
    with pytest.raises(WeightStreamError):
        core.validate_config(config)


def test_config_is_architecture_agnostic():
    assert core.validate_config(b'{"customer_format":"own-v2"}') == {
        "customer_format": "own-v2"
    }
    assert core.validate_config({"arbitrary": [1, 2]}) == {"arbitrary": [1, 2]}


def test_initial_requires_complete_correct_keyframe(monkeypatch):
    monkeypatch.setattr(core.ResidentWeights, "_snapshot", cpu_snapshot)
    initial = initial_manifest()
    for manifest in (
        replace(initial, tensors=initial.tensors[:-1]),
        replace(initial, architecture_hash="0" * 64),
        replace(initial, kind="patch", version=1, parent_version=0),
    ):
        with pytest.raises(WeightStreamError):
            make_resident(TinyModel(), config_bytes(), manifest, bound_layout(initial))


def test_default_snapshot_has_no_public_cpu_bypass():
    with pytest.raises(WeightStreamError, match="CUDA"):
        make_resident(TinyModel(), config_bytes(), initial_manifest())


def test_load_manifest_hash_version_duplicate_and_size_bounds(tmp_path):
    root = tmp_path / "patch"
    digest, manifest = write_patch(root, initial_manifest())
    assert core.load_manifest(root, 1, digest) == manifest
    for version, sha in ((2, digest), (1, "f" * 64), (True, digest), (1, "bad")):
        with pytest.raises(WeightStreamError):
            core.load_manifest(root, version, sha)
    raw = manifest.to_json()[:-1] + b',"version":1}'
    (root / "manifest.json").write_bytes(raw)
    with pytest.raises(WeightStreamError, match="Duplicate"):
        core.load_manifest(root, 1, hashlib.sha256(raw).hexdigest())
    raw = b" " * (core.MAX_MANIFEST_BYTES + 1)
    (root / "manifest.json").write_bytes(raw)
    with pytest.raises(WeightStreamError, match="size"):
        core.load_manifest(root, 1, hashlib.sha256(raw).hexdigest())


def test_sparse_subset_changes_only_selected_values_and_replay_does_not_write(
    resident, tmp_path, monkeypatch
):
    root = tmp_path / "patch"
    digest, _ = write_patch(
        root, initial_manifest(), updates=[("encoder.weight", [1, 127], [1.25, -2])]
    )
    identity = resident.identity_sha256
    ack = resident.apply(root, 1, digest)
    assert ack["ok"] and ack["version"] == 1 and ack["manifest_sha256"] == digest
    assert ack["profile"] == core.PROFILE and ack["identity_sha256"] == identity
    assert (
        ack["sparse_elements"] == 2
        and ack["index_copy_calls"] == 1
        and ack["full_loader_calls"] == 0
    )
    expected = torch.zeros(128, dtype=torch.bfloat16)
    expected[1], expected[127] = 1.25, -2
    assert torch.equal(resident.model.weight, expected)
    monkeypatch.setattr(
        resident, "_write", lambda _: pytest.fail("Replay must not write")
    )
    replay = resident.apply(root, 1, digest)
    assert (
        replay["replayed"]
        and replay["sparse_elements"] == 0
        and replay["index_copy_calls"] == 0
    )
    assert replay["scatter_and_sync_seconds"] == 0
    assert not resident.poisoned


def test_zero_entry_noop_advances_parent_and_replays_without_sync(
    resident, tmp_path, monkeypatch
):
    root = tmp_path / "noop"
    digest, _ = write_patch(root, initial_manifest())
    monkeypatch.setattr(
        resident, "_synchronize", lambda: pytest.fail("No-op must not synchronize")
    )
    ack = resident.apply(root, 1, digest)
    assert (
        ack["version"] == 1
        and ack["sparse_elements"] == 0
        and ack["index_copy_calls"] == 0
    )
    assert resident.apply(root, 1, digest)["replayed"]


def test_strict_parent_and_content_binding_keep_healthy_resident(resident, tmp_path):
    root = tmp_path / "patch"
    digest, manifest = write_patch(root, initial_manifest(), parent=1, version=2)
    with pytest.raises(WeightStreamError, match="parent"):
        resident.apply(root, 2, digest)
    assert not resident.poisoned and resident.version == 0
    digest = save_manifest(root, replace(manifest, parent_version=0, version=1))
    resident.apply(root, 1, digest)
    conflict = save_manifest(
        root, replace(manifest, parent_version=0, version=1, trainer_step=8)
    )
    with pytest.raises(WeightStreamError, match="different manifest"):
        resident.apply(root, 1, conflict)
    assert not resident.poisoned and resident.version == 1


def test_corrupt_final_payload_is_refused_before_any_write(resident, tmp_path):
    root = tmp_path / "patch"
    name = "encoder.weight"
    other = "encoder.bias"
    digest, _ = write_patch(
        root, initial_manifest(), updates=[(name, [1], [2]), (other, [2], [3])]
    )
    payload = bytearray((root / "payload.bin").read_bytes())
    payload[-1] ^= 1
    (root / "payload.bin").write_bytes(payload)
    before = resident.model.weight.detach().clone()
    with pytest.raises(WeightStreamError, match="Checksum"):
        resident.apply(root, 1, digest)
    assert (
        torch.equal(before, resident.model.weight)
        and not resident.poisoned
        and resident.version == 0
    )


def test_nonfinite_payload_refused_even_with_valid_checksum(resident, tmp_path):
    root = tmp_path / "patch"
    digest, _ = write_patch(
        root, initial_manifest(), updates=[("encoder.weight", [1], [float("nan")])]
    )
    with pytest.raises(WeightStreamError, match="Finite"):
        resident.apply(root, 1, digest)
    assert not resident.poisoned and not bool(resident.model.weight.any())


def test_dense_keyframe_and_sparse_budget_refused(resident, tmp_path, monkeypatch):
    initial = initial_manifest()
    root = tmp_path / "patch"
    digest, manifest = write_patch(
        root, initial, updates=[("encoder.weight", None, [0] * 128)]
    )
    with pytest.raises(WeightStreamError, match="restart"):
        resident.apply(root, 1, digest)
    digest = save_manifest(root, replace(initial, version=1, parent_version=0))
    with pytest.raises(WeightStreamError, match="restart"):
        resident.apply(root, 1, digest)
    sparse = replace(manifest.tensors[0], encoding="sparse", nnz=2)
    digest = save_manifest(root, replace(manifest, tensors=(sparse,)))
    resident._max_sparse_elements = 1
    with pytest.raises(WeightStreamError, match="element bound"):
        resident.apply(root, 1, digest)
    assert not resident.poisoned


def test_payload_file_budget_refused_before_decode(resident, tmp_path, monkeypatch):
    root = tmp_path / "patch"
    digest, _ = write_patch(
        root, initial_manifest(), updates=[("encoder.weight", [1], [2])]
    )
    monkeypatch.setattr(core, "MAX_PAYLOAD_BYTES", 1)
    with pytest.raises(WeightStreamError, match="compressed"):
        resident.apply(root, 1, digest)
    assert not resident.poisoned


@pytest.mark.parametrize(
    "failure", ["before_sync", "during_write", "after_sync", "identity"]
)
def test_uncertain_write_or_identity_permanently_poisons(
    resident, tmp_path, monkeypatch, failure
):
    root = tmp_path / "patch"
    digest, _ = write_patch(
        root, initial_manifest(), updates=[("encoder.weight", [1], [2])]
    )
    if failure == "identity":
        resident.model.weight = torch.nn.Parameter(
            resident.model.weight.detach().clone()
        )
    elif failure == "during_write":

        def write_then_fail(prepared):
            with torch.no_grad():
                resident.model.weight[1] = 2
            raise RuntimeError("uncertain write")

        monkeypatch.setattr(resident, "_write", write_then_fail)
    else:
        calls = []

        def synchronize():
            calls.append(1)
            if failure == "before_sync" or len(calls) == 2:
                raise RuntimeError("uncertain sync")

        monkeypatch.setattr(resident, "_synchronize", synchronize)
    with pytest.raises((WeightStreamError, RuntimeError)):
        resident.apply(root, 1, digest)
    assert resident.poisoned and resident.version == 0
    with pytest.raises(WeightStreamError, match="poisoned"):
        resident.apply(root, 1, digest)


def test_all_payloads_decoded_before_first_mutation(resident, tmp_path, monkeypatch):
    root = tmp_path / "patch"
    digest, manifest = write_patch(
        root,
        initial_manifest(),
        updates=[
            ("encoder.weight", [1], [2]),
            ("encoder.bias", [2], [3]),
        ],
    )
    original = core.read_patch
    calls = []

    def refuse_last(directory, entry):
        calls.append(entry.name)
        if entry is manifest.tensors[-1] or entry.name == manifest.tensors[-1].name:
            raise WeightStreamError("late decode refusal")
        return original(directory, entry)

    monkeypatch.setattr(core, "read_patch", refuse_last)
    monkeypatch.setattr(
        resident,
        "_write",
        lambda _: pytest.fail("Late decode failure must precede every write"),
    )
    with pytest.raises(WeightStreamError, match="late decode"):
        resident.apply(root, 1, digest)
    assert (
        len(calls) == 2
        and not resident.poisoned
        and not bool(resident.model.weight.any())
    )


class LayoutParameter:
    """Metadata-only native layout double; never allocates model/GPU storage."""

    def __init__(self, shape, identity):
        self.shape = shape
        self.dtype = torch.bfloat16
        self.device = torch.device("cuda:0")
        self.pointer = identity * 2**40
        self.contiguous = True

    def is_contiguous(self):
        return self.contiguous

    def untyped_storage(self):
        return SimpleNamespace(
            data_ptr=lambda: self.pointer, nbytes=lambda: math.prod(self.shape) * 2
        )

    def data_ptr(self):
        return self.pointer

    def stride(self):
        return tuple(
            math.prod(self.shape[index + 1 :]) for index in range(len(self.shape))
        )

    def storage_offset(self):
        return 0

    def numel(self):
        return math.prod(self.shape)

    def element_size(self):
        return 2


def layout_model():
    parameters = {
        name: LayoutParameter(shape, index + 1)
        for index, (name, shape) in enumerate(NATIVE_SCHEMA.items())
    }
    return SimpleNamespace(
        parameters=parameters,
        named_parameters=lambda **kwargs: list(parameters.items()),
    )


@pytest.mark.parametrize(
    "wrong",
    [
        "name",
        "shape",
        "dtype",
        "cpu",
        "stride",
        "alias",
        "device",
        "offset",
        "oversized_storage",
        "view_pointer",
    ],
)
def test_real_snapshot_refuses_wrong_native_metadata(monkeypatch, wrong):
    monkeypatch.setattr(core, "_process_start_ticks", lambda: "test-start")
    model = layout_model()
    parameter = model.parameters["encoder.weight"]
    if wrong == "name":
        model.parameters["wrong.name"] = model.parameters.pop("encoder.weight")
    elif wrong == "shape":
        parameter.shape = (64,)
    elif wrong == "dtype":
        parameter.dtype = torch.float16
    elif wrong == "cpu":
        parameter.device = torch.device("cpu")
    elif wrong == "stride":
        parameter.contiguous = False
    elif wrong == "alias":
        parameter.pointer = model.parameters["encoder.bias"].pointer
    elif wrong == "offset":
        parameter.storage_offset = lambda: 1
    elif wrong == "oversized_storage":
        parameter.untyped_storage = lambda: SimpleNamespace(
            data_ptr=lambda: parameter.pointer,
            nbytes=lambda: math.prod(parameter.shape) * 2 + 2,
        )
    elif wrong == "view_pointer":
        parameter.data_ptr = lambda: parameter.pointer + 2
    else:
        parameter.device = torch.device("cuda:1")
    with pytest.raises(WeightStreamError):
        make_resident(model, config_bytes(), initial_manifest())


def test_real_snapshot_detects_storage_replacement_and_poisons(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "_process_start_ticks", lambda: "test-start")
    model = layout_model()
    resident = make_resident(model, config_bytes(), initial_manifest())
    digest, _ = write_patch(tmp_path / "noop", initial_manifest())
    model.parameters["encoder.weight"].pointer += 2**39
    with pytest.raises(WeightStreamError, match="identity"):
        resident.apply(tmp_path / "noop", 1, digest)
    assert resident.poisoned


def keyframe_files(directory, tensors, config=None, schema=None):
    from safetensors.torch import save_file

    directory.mkdir()
    config = config or config_bytes()
    schema = schema or LOGICAL_SCHEMA
    (directory / "config.json").write_bytes(config)
    save_file(tensors, str(directory / "model.safetensors"))
    payload = (directory / "model.safetensors").read_bytes()
    manifest = WeightManifest(
        0,
        None,
        "keyframe",
        MODEL_ID,
        architecture_hash(
            config,
            (
                (name, SimpleNamespace(shape=shape, dtype="bfloat16"))
                for name, shape in schema.items()
            ),
        ),
        tuple(
            TensorEntry(
                name,
                "bfloat16",
                shape,
                "dense",
                "model.safetensors",
                0,
                len(payload),
                math.prod(shape),
                checksum(payload),
            )
            for name, shape in schema.items()
        ),
    )
    save_manifest(directory, manifest)
    return manifest


@pytest.fixture
def initial_cpu(monkeypatch, tmp_path):
    monkeypatch.setattr(core.ResidentWeights, "_snapshot", cpu_snapshot)
    monkeypatch.setattr(core.ResidentWeights, "_synchronize", lambda self: None)
    model = TinyModel()
    directory = tmp_path / "initial"
    initial = keyframe_files(
        directory,
        {name: value.detach().clone() for name, value in model.named_parameters()},
    )
    resident = make_resident(model, config_bytes(), initial)
    return resident, directory


def test_initial_audit_required_then_real_cpu_byte_proof(initial_cpu, tmp_path):
    resident, directory = initial_cpu
    digest, _ = write_patch(tmp_path / "patch", resident._initial)
    assert not resident.initial_verified
    with pytest.raises(WeightStreamError, match="byte audit"):
        resident.apply(tmp_path / "patch", 1, digest)
    proof = resident.verify_initial(directory)
    assert (
        proof["ok"]
        and proof["logical_tensors"] == 2
        and len(proof["base_sha256"]) == 64
    )
    assert proof["identity_sha256"] == resident.identity_sha256
    assert (
        proof["manifest_sha256"]
        == hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()
    )
    assert resident.initial_verified
    resident.apply(tmp_path / "patch", 1, digest)
    with pytest.raises(WeightStreamError, match="advanced"):
        resident.verify_initial(directory)
    assert resident.initial_verified and resident.version == 1


@pytest.mark.parametrize(
    "wrong", ["value", "signed_zero", "manifest", "config", "corrupt_payload"]
)
def test_initial_audit_refusal_leaves_unverified(initial_cpu, wrong):
    resident, directory = initial_cpu
    if wrong == "value":
        with torch.no_grad():
            resident.model.weight[0] = 1
    elif wrong == "signed_zero":
        with torch.no_grad():
            resident.model.weight[0] = -0.0
    elif wrong == "manifest":
        save_manifest(directory, replace(resident._initial, trainer_step=99))
    elif wrong == "config":
        (directory / "config.json").write_bytes(config_bytes() + b"\n")
    else:
        payload = bytearray((directory / "model.safetensors").read_bytes())
        payload[-1] ^= 1
        (directory / "model.safetensors").write_bytes(payload)
    with pytest.raises(WeightStreamError):
        resident.verify_initial(directory)
    assert not resident.initial_verified


@pytest.mark.parametrize("wrong", ["catalog", "dtype", "shape"])
def test_initial_safetensors_catalog_shape_dtype_checked(monkeypatch, tmp_path, wrong):
    monkeypatch.setattr(core.ResidentWeights, "_snapshot", cpu_snapshot)
    tensor = torch.zeros(
        64 if wrong == "shape" else 128,
        dtype=torch.float32 if wrong == "dtype" else torch.bfloat16,
    )
    tensors = {
        "encoder.weight": tensor,
        "encoder.bias": torch.zeros(128, dtype=torch.bfloat16),
    }
    if wrong == "catalog":
        tensors["unexpected.weight"] = torch.zeros(1)
    directory = tmp_path / "initial"
    manifest = keyframe_files(directory, tensors)
    resident = make_resident(TinyModel(), config_bytes(), manifest)
    with pytest.raises(WeightStreamError, match="catalog|shape or dtype"):
        resident.verify_initial(directory)
    assert not resident.initial_verified


def test_initial_manifest_semantic_equality_allows_whitespace(initial_cpu):
    resident, directory = initial_cpu
    (directory / "manifest.json").write_bytes(resident._initial.to_json() + b"\n")
    assert resident.verify_initial(directory)["ok"]


def test_initial_packed_audit_is_independent_of_writer_mapping(monkeypatch, tmp_path):
    from chalkcompute.weights.layout import CompiledLayout

    packed = torch.arange(256, dtype=torch.float32).to(torch.bfloat16).reshape(256, 1)
    tensors = {"left": packed[:128].clone(), "right": packed[128:].clone()}
    directory = tmp_path / "initial"
    manifest = keyframe_files(
        directory,
        tensors,
        schema={name: tuple(value.shape) for name, value in tensors.items()},
    )
    layout = WeightLayout(
        rules=[TensorMapping(["left", "right"], "packed", [Concatenate(0)])],
        match_by_name=False,
    ).bind(WeightSchema.from_manifest(manifest))
    parameter = torch.nn.Parameter(packed)
    model = SimpleNamespace(named_parameters=lambda **kwargs: [("packed", parameter)])
    monkeypatch.setattr(core.ResidentWeights, "_snapshot", cpu_snapshot)
    monkeypatch.setattr(
        CompiledLayout,
        "map_indices",
        lambda *a, **k: pytest.fail("Initial audit must use independent inverse"),
    )
    resident = make_resident(model, config_bytes(), manifest, layout)
    proof = resident.verify_initial(directory)
    assert proof["logical_tensors"] == 2
    assert proof["schema_sha256"] == resident.schema_sha256
    assert proof["layout_sha256"] == layout.sha256


def test_zero_nnz_sparse_entry_is_a_valid_noop(resident, tmp_path):
    directory = tmp_path / "patch"
    digest, _ = write_patch(
        directory, resident._initial, updates=[("encoder.weight", [], [])]
    )
    ack = resident.apply(directory, 1, digest)
    assert ack["ok"] and ack["sparse_elements"] == ack["index_copy_calls"] == 0


def test_chunked_actual_indexed_writes(resident, tmp_path, monkeypatch):
    directory = tmp_path / "patch"
    digest, _ = write_patch(
        directory,
        resident._initial,
        updates=[("encoder.weight", [1, 2, 3], [2, 4, 6])],
    )
    monkeypatch.setattr(core, "_CHUNK_ELEMENTS", 1)
    ack = resident.apply(directory, 1, digest)
    assert ack["index_copy_calls"] == 3 and ack["sparse_elements"] == 3
    assert resident.model.weight[1:4].tolist() == [2, 4, 6]


def test_dispatch_guard_refuses_nonindexed_mutation(resident, tmp_path, monkeypatch):
    directory = tmp_path / "patch"
    digest, _ = write_patch(
        directory, resident._initial, updates=[("encoder.weight", [1], [2])]
    )
    monkeypatch.setattr(
        torch.Tensor, "index_copy_", lambda self, *args: self.copy_(self)
    )
    with pytest.raises(WeightStreamError, match="Only indexed"):
        resident.apply(directory, 1, digest)
    assert resident.poisoned and resident.version == 0


def test_identity_change_after_actual_write_poisons(resident, tmp_path, monkeypatch):
    directory = tmp_path / "patch"
    digest, _ = write_patch(
        directory, resident._initial, updates=[("encoder.weight", [1], [2])]
    )
    original = resident._write

    def write_and_replace(prepared):
        result = original(prepared)
        resident.model.weight = torch.nn.Parameter(
            resident.model.weight.detach().clone()
        )
        return result

    monkeypatch.setattr(resident, "_write", write_and_replace)
    with pytest.raises(WeightStreamError, match="identity"):
        resident.apply(directory, 1, digest)
    assert resident.poisoned and resident.version == 0


def test_reverification_failure_revokes_initial_verified_flag(initial_cpu):
    resident, directory = initial_cpu
    resident.verify_initial(directory)
    (directory / "config.json").write_bytes(b"{}")
    with pytest.raises(WeightStreamError):
        resident.verify_initial(directory)
    assert not resident.initial_verified


def test_failure_constructing_ack_after_write_poisons_before_state_commit(
    resident, tmp_path, monkeypatch
):
    directory = tmp_path / "patch"
    digest, _ = write_patch(
        directory, resident._initial, updates=[("encoder.weight", [1], [2])]
    )
    calls = []

    def clock():
        calls.append(1)
        if len(calls) == 5:
            raise RuntimeError("ACK construction interrupted")
        return len(calls)

    monkeypatch.setattr(core.time, "monotonic", clock)
    with pytest.raises(RuntimeError, match="ACK construction"):
        resident.apply(directory, 1, digest)
    assert resident.model.weight[1] == 2 and resident.version == 0 and resident.poisoned


def test_public_profile_input_reader_bounds_and_whitelist(tmp_path):
    (tmp_path / "config.json").write_bytes(b"{}")
    assert core.read_profile_file(tmp_path, "config.json") == b"{}"
    with pytest.raises(WeightStreamError, match="Only resident"):
        core.read_profile_file(tmp_path, "payload.bin")
    (tmp_path / "config.json").write_bytes(b" " * (core.MAX_MANIFEST_BYTES + 1))
    with pytest.raises(WeightStreamError, match="size bound"):
        core.read_profile_file(tmp_path, "config.json")


def test_source_bound_layout_required(monkeypatch):
    monkeypatch.setattr(core.ResidentWeights, "_snapshot", cpu_snapshot)
    for layout in (WeightLayout(), WeightLayout(source_schema_sha256="f" * 64)):
        with pytest.raises(WeightStreamError, match="bound"):
            core.ResidentWeights(
                TinyModel(), config_bytes(), initial_manifest(), layout
            )


def test_custom_model_name_preserved_and_patch_cannot_change_it(resident, tmp_path):
    digest, manifest = write_patch(tmp_path / "patch", resident._initial)
    assert resident._initial.base_model == MODEL_ID
    digest = save_manifest(
        tmp_path / "patch", replace(manifest, base_model="another/customer-model")
    )
    with pytest.raises(WeightStreamError, match="bound resident schema"):
        resident.apply(tmp_path / "patch", 1, digest)
    assert not resident.poisoned and resident.version == 0


def test_sparse_limit_derived_from_model_size(resident, tmp_path):
    assert resident._total_elements == 256 and resident._max_sparse_elements == 5
    digest, _ = write_patch(
        tmp_path / "patch",
        resident._initial,
        updates=[("encoder.weight", list(range(6)), [1] * 6)],
    )
    with pytest.raises(WeightStreamError, match="element bound"):
        resident.apply(tmp_path / "patch", 1, digest)
    assert resident.version == 0 and not resident.poisoned


def test_schema_and_layout_are_bound_into_real_identity(monkeypatch):
    monkeypatch.setattr(core, "_process_start_ticks", lambda: "test-start")
    resident = make_resident(layout_model(), config_bytes(), initial_manifest())
    assert resident._identity["schema_sha256"] == resident.schema_sha256
    assert resident._identity["layout_sha256"] == resident.layout_sha256
    assert len(resident._identity["parameters"]) == 2


def test_column_concat_transpose_real_codec_audit_and_sparse_write(
    monkeypatch, tmp_path
):
    from chalkcompute.weights.layout import Transpose

    monkeypatch.setattr(core.ResidentWeights, "_snapshot", cpu_snapshot)
    monkeypatch.setattr(core.ResidentWeights, "_synchronize", lambda self: None)
    tensors = {
        "custom.left": torch.arange(128).to(torch.bfloat16).reshape(4, 32),
        "custom.right": torch.arange(128, 256).to(torch.bfloat16).reshape(4, 32),
    }
    schema = {name: tuple(value.shape) for name, value in tensors.items()}
    initial_dir = tmp_path / "initial"
    initial = keyframe_files(initial_dir, tensors, schema=schema)
    layout = WeightLayout(
        rules=[
            TensorMapping(
                ["custom.left", "custom.right"],
                "native.packed",
                [Concatenate(1), Transpose(0, 1)],
            )
        ],
        match_by_name=False,
    ).bind(WeightSchema.from_manifest(initial))
    parameter = torch.nn.Parameter(
        torch.cat(list(tensors.values()), dim=1).t().contiguous()
    )
    model = SimpleNamespace(
        named_parameters=lambda **kwargs: [("native.packed", parameter)]
    )
    resident = make_resident(model, config_bytes(), initial, layout)
    proof = resident.verify_initial(initial_dir)
    assert proof["layout_sha256"] == layout.sha256
    directory = tmp_path / "patch"
    digest, _ = write_patch(
        directory,
        initial,
        updates=[
            ("custom.left", [0, 33], [-2, -3]),
            ("custom.right", [127], [-4]),
        ],
    )
    ack = resident.apply(directory, 1, digest)
    assert ack["sparse_elements"] == 3 and ack["full_loader_calls"] == 0
    assert ack["layout_sha256"] == proof["layout_sha256"]
    assert ack["schema_sha256"] == proof["schema_sha256"]
    tensors["custom.left"].reshape(-1)[[0, 33]] = torch.tensor(
        [-2, -3], dtype=torch.bfloat16
    )
    tensors["custom.right"].reshape(-1)[127] = -4
    expected = torch.cat(list(tensors.values()), dim=1).t().contiguous()
    assert torch.equal(parameter.view(torch.uint8), expected.view(torch.uint8))
    assert resident.apply(directory, 1, digest)["replayed"]
