"""Generic coordinate compiler tests with independently executed dense oracles."""

import json
import random
import subprocess
import sys
from types import SimpleNamespace

import pytest

from chalkcompute.weights.layout import (
    Concatenate,
    Interleave,
    Reshape,
    Split,
    TensorMapping,
    TensorSpec,
    Transpose,
    WeightLayout,
    WeightSchema,
)

torch = pytest.importorskip("torch")


def schema(values):
    return WeightSchema(
        tuple(
            TensorSpec(name, tuple(t.shape), str(t.dtype).removeprefix("torch."))
            for name, t in values.items()
        )
    )


def check(source, target, operations):
    source_schema, target_schema = schema(source), schema(target)
    layout = WeightLayout(
        [TensorMapping(list(source), list(target), operations)], match_by_name=False
    ).bind(source_schema)
    compiled = layout.compile(source_schema, target_schema)
    # Test nonmonotonic coordinates and duplicates as well as full coverage.
    for name, tensor in source.items():
        indices = torch.arange(tensor.numel() - 1, -1, -1, dtype=torch.int64)
        if indices.numel():
            indices = torch.cat((indices, indices[:1]))
        routes = compiled.map_indices(name, indices)
        assert sum(route.indices.numel() for route in routes) == indices.numel()
        for route in routes:
            assert torch.equal(
                target[route.target].reshape(-1)[route.indices],
                tensor.reshape(-1)[indices[route.source_positions]],
            )
        assert torch.equal(compiled.read_source(name, target.__getitem__), tensor)
    assert WeightLayout.from_json(layout.to_json()) == layout
    assert WeightLayout.from_json(layout.to_json()).sha256 == layout.sha256
    assert WeightSchema.from_json(source_schema.to_json()) == source_schema
    return compiled


def test_lazy_public_import_does_not_import_optional_libraries():
    code = 'import sys; from chalkcompute.weights import WeightLayout, WeightSchema, TensorMapping, TensorSpec, Concatenate, Split, Transpose, Reshape, Interleave; assert not any(n in sys.modules for n in ("torch", "vllm", "sglang", "chalkcompute._weights"))'
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("dim", [0, 1, -1])
def test_row_and_column_concatenation(dim):
    shape = (2, 3)
    a = torch.arange(6).reshape(shape)
    bshape = list(shape)
    bshape[dim] = 4
    b = torch.arange(torch.tensor(bshape).prod()).reshape(bshape) + 100
    check(
        {"left": a, "right": b},
        {"packed": torch.cat((a, b), dim=dim)},
        [Concatenate(dim)],
    )


def test_transpose_reshape_split_chain():
    value = torch.arange(24).reshape(4, 6)
    transformed = value.transpose(0, 1).reshape(3, 8)
    left, right = torch.split(transformed, [3, 5], dim=1)
    check(
        {"logical": value},
        {"l": left, "r": right},
        [Transpose(0, 1), Reshape((3, 8)), Split([3, 5], 1)],
    )


def test_seeded_operation_compositions_against_dense_oracle():
    """Exercise mixed layouts, including empty split pieces and negative axes."""
    for seed in range(100):
        rng = random.Random(seed)
        columns = rng.randint(1, 5)
        source = {
            f"source{i}": torch.arange(rows * columns).reshape(rows, columns) + 1000 * i
            for i, rows in enumerate(rng.choices(range(1, 6), k=rng.randint(1, 3)))
        }
        values = list(source.values())
        operations = []
        for _ in range(7):
            if len(values) > 1:
                # Splits may have produced zero-width pieces. Their positive
                # common divisors still define a byte-preserving interleave.
                axis = next(
                    (
                        d
                        for d in range(values[0].ndim)
                        if all(
                            tuple(v.shape[:d]) + tuple(v.shape[d + 1 :])
                            == tuple(values[0].shape[:d])
                            + tuple(values[0].shape[d + 1 :])
                            for v in values
                        )
                    ),
                    0,
                )
                divisors = [
                    g
                    for g in range(1, 5)
                    if all(v.shape[axis] % g == 0 for v in values)
                ]
                groups = rng.choice(divisors)
                if rng.choice((True, False)):
                    operations.append(Interleave(axis, groups))
                    pieces = [
                        torch.split(v, [v.shape[axis] // groups] * groups, dim=axis)
                        for v in values
                    ]
                    values = [
                        torch.cat(
                            [p[g] for g in range(groups) for p in pieces], dim=axis
                        )
                    ]
                else:
                    operations.append(Concatenate(axis))
                    values = [torch.cat(values, dim=axis)]
            else:
                value = values[0]
                action = rng.choice(("transpose", "reshape", "split"))
                if action == "transpose":
                    operations.append(Transpose(0, -1))
                    values = [value.transpose(0, -1)]
                elif action == "reshape":
                    shape = (
                        (1, value.numel())
                        if rng.choice((True, False))
                        else (value.numel(), 1)
                    )
                    operations.append(Reshape(shape))
                    values = [value.reshape(shape)]
                else:
                    axis = rng.randrange(value.ndim)
                    cut = rng.randint(0, value.shape[axis])
                    sizes = [cut, value.shape[axis] - cut]
                    operations.append(Split(sizes, axis))
                    values = list(torch.split(value, sizes, dim=axis))
        check(source, {f"target{i}": v for i, v in enumerate(values)}, operations)


def test_split_concat_transpose_round_trip():
    value = torch.arange(24).reshape(4, 6)
    pieces = torch.split(value, [1, 3], dim=0)
    check(
        {"x": value},
        {"y": torch.cat(pieces, dim=0).t()},
        [Split([1, 3], 0), Concatenate(0), Transpose(0, 1)],
    )


@pytest.mark.parametrize("dim", [0, 1])
def test_unequal_group_interleave(dim):
    # Deliberately generic three-component layout, unequal group widths.
    shapes = [(8, 3), (4, 3), (4, 3)] if dim == 0 else [(3, 8), (3, 4), (3, 4)]
    inputs = {
        name: torch.arange(a * b).reshape(a, b) + i * 100
        for i, (name, (a, b)) in enumerate(zip(("a", "b", "c"), shapes))
    }
    chunks = [torch.chunk(t, 4, dim=dim) for t in inputs.values()]
    packed = torch.cat(
        [chunks[slot][group] for group in range(4) for slot in range(3)], dim=dim
    )
    compiled = check(inputs, {"packed": packed}, [Interleave(dim, 4)])
    # The independent inverse must not call the sparse coordinate path.
    original = type(compiled).map_indices
    try:
        type(compiled).map_indices = lambda *args: (_ for _ in ()).throw(
            AssertionError("Not an independent audit")
        )
        assert torch.equal(
            compiled.read_source("a", {"packed": packed}.__getitem__), inputs["a"]
        )
    finally:
        type(compiled).map_indices = original


def test_scalar_reshape_and_identity():
    check({"a": torch.tensor(9)}, {"b": torch.tensor([9])}, [Reshape((1,))])
    s = WeightSchema([TensorSpec("value", (1_000_000_000_000,), "bfloat16")])
    compiled = WeightLayout().bind(s).compile(s, s)
    # Metadata-only compile: a trillion elements requires no index table.
    route = compiled.map_indices("value", torch.tensor([999_999_999_999]))[0]
    assert route.indices.tolist() == [999_999_999_999]


def test_wildcard_and_identity_fallthrough():
    src = WeightSchema(
        [TensorSpec(n, (2, 3), "float32") for n in ("layer.0.w", "layer.12.w", "norm")]
    )
    dst = WeightSchema(
        [TensorSpec(n, (3, 2), "float32") for n in ("block.0.w", "block.12.w")]
        + [TensorSpec("norm", (2, 3), "float32")]
    )
    layout = WeightLayout(
        [TensorMapping("layer.*.w", "block.*.w", [Transpose(0, 1)])]
    ).bind(src)
    compiled = layout.compile(src, dst)
    assert (
        compiled.map_indices("layer.12.w", torch.tensor([1]))[0].target == "block.12.w"
    )
    assert compiled.map_indices("norm", torch.tensor([1]))[0].target == "norm"


def test_schema_hash_canonical_order_and_manifest_binding():
    a, b = TensorSpec("a", (2,), "bfloat16"), TensorSpec("b", (3,), "bfloat16")
    s = WeightSchema([a, b])
    assert s.sha256 == WeightSchema([b, a]).sha256
    assert s == WeightSchema.from_manifest(
        SimpleNamespace(kind="keyframe", tensors=[b, a])
    )
    with pytest.raises(ValueError):
        WeightSchema.from_manifest(SimpleNamespace(kind="patch", tensors=[a]))
    bound = WeightLayout().bind(s)
    with pytest.raises(ValueError):
        bound.bind(WeightSchema([a]))
    with pytest.raises(ValueError):
        WeightLayout().compile(s, s)
    assert bound.source_schema_sha256 == s.sha256


@pytest.mark.parametrize(
    "mutate",
    [
        lambda x: x.update(version=2),
        lambda x: x.update(version=True),
        lambda x: x.update(callback="evil"),
        lambda x: x.update(match_by_name=1),
        lambda x: x.update(source_schema_sha256="bad"),
        lambda x: x.update(
            rules=[
                {
                    "sources": ["a"],
                    "target": ["b"],
                    "operations": [{"op": "python", "code": "evil"}],
                }
            ]
        ),
    ],
)
def test_strict_recipe_parser(mutate):
    obj = json.loads(WeightLayout().to_json())
    mutate(obj)
    with pytest.raises(ValueError):
        WeightLayout.from_json(json.dumps(obj))


def test_duplicate_and_nonfinite_json_rejected():
    for raw in (
        '{"version":1,"version":1}',
        '{"version":NaN}',
        "[]",
        "x" * (4 * 1024 * 1024 + 1),
    ):
        with pytest.raises(ValueError):
            WeightLayout.from_json(raw)
    with pytest.raises(ValueError):
        WeightSchema.from_json('{"version":1,"tensors":[],"extra":0}')


@pytest.mark.parametrize(
    "rules,fallthrough",
    [
        ([], False),
        ([TensorMapping("a", "x"), TensorMapping("a", "y")], True),
        ([TensorMapping("a", "x"), TensorMapping("b", "x")], True),
        ([TensorMapping("missing", "x")], True),
    ],
)
def test_coverage_overlap_and_missing_refused(rules, fallthrough):
    src = WeightSchema([TensorSpec(n, (2,), "float32") for n in ("a", "b")])
    dst = WeightSchema([TensorSpec(n, (2,), "float32") for n in ("x", "y")])
    with pytest.raises(ValueError):
        WeightLayout(rules, fallthrough).bind(src).compile(src, dst)


@pytest.mark.parametrize(
    "op,source_shapes,target_shapes",
    [
        (Concatenate(0), [(2, 3), (2, 4)], [(4, 3)]),
        (Split([1], 0), [(2, 3)], [(1, 3)]),
        (Transpose(2, 0), [(2, 3)], [(3, 2)]),
        (Reshape((7,)), [(2, 3)], [(7,)]),
        (Interleave(0, 3), [(2, 3), (2, 3)], [(4, 3)]),
    ],
)
def test_invalid_shapes_and_operations_refused(op, source_shapes, target_shapes):
    src = WeightSchema(
        [TensorSpec(str(i), s, "float32") for i, s in enumerate(source_shapes)]
    )
    dst = WeightSchema(
        [TensorSpec("out" + str(i), s, "float32") for i, s in enumerate(target_shapes)]
    )
    with pytest.raises(ValueError):
        WeightLayout(
            [
                TensorMapping(
                    [t.name for t in src.tensors], [t.name for t in dst.tensors], [op]
                )
            ],
            False,
        ).bind(src).compile(src, dst)


def test_dtype_and_actual_destination_validation():
    src = WeightSchema([TensorSpec("a", (2,), "float32")])
    dst = WeightSchema([TensorSpec("b", (2,), "float16")])
    with pytest.raises(ValueError):
        WeightLayout([TensorMapping("a", "b")]).bind(src).compile(src, dst)
    compiled = WeightLayout().bind(src).compile(src, src)
    with pytest.raises(ValueError):
        compiled.read_source("a", lambda _: torch.ones(2, dtype=torch.float16))
    for bad in (
        torch.tensor([-1]),
        torch.tensor([2]),
        torch.tensor([0.0]),
        torch.tensor([[0]]),
    ):
        with pytest.raises(ValueError):
            compiled.map_indices("a", bad)
    with pytest.raises(ValueError):
        TensorMapping("a", "b", [lambda x: x])
    with pytest.raises(ValueError):
        TensorSpec("a", (1 << 63,), "float32")


def test_zero_element_and_empty_patch_vectors():
    compiled = check(
        {"a": torch.empty(0, 3), "b": torch.empty(0, 3)},
        {"packed": torch.empty(0, 3)},
        [Interleave(0, 2)],
    )
    assert compiled.map_indices("a", torch.empty(0, dtype=torch.int64)) == []


def test_empty_interleave_cannot_expand_unbounded_metadata():
    source = WeightSchema([TensorSpec("a", (0, 3), "bfloat16")])
    target = WeightSchema([TensorSpec("packed", (0, 3), "bfloat16")])
    layout = WeightLayout(
        [TensorMapping("a", "packed", [Interleave(0, (1 << 63) - 1)])],
        match_by_name=False,
    ).bind(source)
    with pytest.raises(ValueError, match="interleave pieces"):
        layout.compile(source, target)


def test_expanded_duplicate_names_rejected():
    src = WeightSchema([TensorSpec("a.00", (1,), "float32")])
    dst = WeightSchema([TensorSpec("b.0", (2,), "float32")])
    rule = TensorMapping(["a.*0", "a.0*"], "b.*", [Concatenate()])
    with pytest.raises(ValueError, match="duplicate"):
        WeightLayout([rule], False).bind(src).compile(src, dst)


def test_split_target_cannot_also_take_identity_fallthrough():
    source = WeightSchema(
        [TensorSpec("x", (4,), "float32"), TensorSpec("a", (2,), "float32")]
    )
    target = WeightSchema(
        [TensorSpec("a", (2,), "float32"), TensorSpec("b", (2,), "float32")]
    )
    with pytest.raises(ValueError, match="cover"):
        WeightLayout([TensorMapping("x", ["a", "b"], [Split([2, 2])])]).bind(
            source
        ).compile(source, target)


def test_operation_field_and_dimension_bounds():
    for invalid in (Concatenate(True), Transpose(0, 1.0)):
        with pytest.raises(ValueError):
            TensorMapping("a", "b", [invalid])
    for factory in (
        lambda: Interleave(0, 0),
        lambda: Reshape((-1,)),
        lambda: Split([-1]),
        lambda: TensorSpec("a", (True,), "float32"),
    ):
        with pytest.raises(ValueError):
            factory()
    obj = json.loads(WeightLayout([TensorMapping("a", "b", [Concatenate()])]).to_json())
    obj["rules"][0]["operations"][0]["callable"] = "not allowed"
    with pytest.raises(ValueError):
        WeightLayout.from_json(json.dumps(obj))


def test_compile_has_no_optional_import_or_element_sized_allocation():
    code = """
import sys
from chalkcompute.weights import WeightSchema, TensorSpec, WeightLayout, TensorMapping, Transpose
source = WeightSchema([TensorSpec('x', [1000000000000, 2], 'bfloat16')])
target = WeightSchema([TensorSpec('y', [2, 1000000000000], 'bfloat16')])
compiled = WeightLayout(rules=[TensorMapping(sources='x', target='y', operations=[Transpose(0,1)])], match_by_name=False).bind(source).compile(source, target)
assert len(compiled._programs) == 1 and len(compiled._lookup) == 1
assert not any(n in sys.modules for n in ('torch', 'vllm', 'sglang', 'chalkcompute._weights'))
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr


def test_mapping_work_scales_with_patch_and_keeps_cpu_indices(monkeypatch):
    source = WeightSchema([TensorSpec("x", (1000000000000, 2), "bfloat16")])
    target = WeightSchema([TensorSpec("y", (2, 1000000000000), "bfloat16")])
    compiled = (
        WeightLayout([TensorMapping("x", "y", [Transpose(0, 1)])], False)
        .bind(source)
        .compile(source, target)
    )
    indices = torch.tensor([0, 1999999999999, 6], dtype=torch.int64)
    observed = []
    real_arange = torch.arange

    def bounded_arange(count, **kwargs):
        observed.append(count)
        assert count == 3
        return real_arange(count, **kwargs)

    monkeypatch.setattr(torch, "arange", bounded_arange)
    # A process-wide default device must not redirect CPU patch bookkeeping.
    with torch.device("meta"):
        route = compiled.map_indices("x", indices)[0]
    assert observed == [3]
    assert route.indices.device.type == route.source_positions.device.type == "cpu"
    assert route.indices.tolist() == [0, 1999999999999, 3]
