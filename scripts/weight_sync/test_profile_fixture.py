import math

import pytest

from profile_fixture import (
    AUDIT_NAMES,
    change_counts,
    digest,
    mutate,
    require_target_changed,
    verify_native,
)


def test_exact_global_one_percent_and_deterministic_remainders():
    shapes = {f"w{i}": (128 + i,) for i in range(399)}
    counts = change_counts(shapes)
    assert sum(counts.values()) == sum(math.prod(s) for s in shapes.values()) // 100
    assert all(count > 0 for count in counts.values())
    assert counts == change_counts(dict(reversed(list(shapes.items()))))


def test_full_8b_exact_count():
    assert change_counts({"a": (8190735360,)}) == {"a": 81907353}


@pytest.mark.parametrize("shape", [{}, {"bad": (0,)}, {"bad": (-1,)}])
def test_rejects_invalid_shapes(shape):
    with pytest.raises(ValueError):
        change_counts(shape)


def test_finite_bf16_bitflip_is_exact_reversible_and_changes_digest():
    torch = pytest.importorskip("torch")
    source = torch.tensor(
        [
            0.0,
            -0.0,
            1.0,
            -2.0,
            torch.finfo(torch.bfloat16).max,
            torch.finfo(torch.bfloat16).tiny,
        ],
        dtype=torch.bfloat16,
    )
    target = mutate(source, 3)
    assert torch.isfinite(target).all()
    assert digest(target) != digest(source)
    restored = mutate(target, 3)
    assert digest(restored) == digest(source)
    assert torch.equal(
        source.view(torch.int16).bitwise_and(-2),
        target.view(torch.int16).bitwise_and(-2),
    )
    assert digest(source) == digest(mutate(source, 0))


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_nonfinite_source_rejected(bad):
    torch = pytest.importorskip("torch")
    with pytest.raises(ValueError, match="nonfinite"):
        mutate(torch.tensor([bad], dtype=torch.bfloat16), 1)


def test_exact_native_mapping_before_mutation_and_restore():
    base = {name: "a" * 64 for name in AUDIT_NAMES}
    target = {name: "b" * 64 for name in AUDIT_NAMES}
    require_target_changed(base, target)
    assert verify_native({"results": [base]}, base)
    assert verify_native({"results": [target]}, target)
    assert verify_native({"results": [base]}, base)
    with pytest.raises(ValueError, match="mapping"):
        verify_native({"results": [target]}, base)
    with pytest.raises(ValueError, match="acknowledgements"):
        verify_native({"results": []}, base)
    with pytest.raises(ValueError, match="actually change"):
        require_target_changed(base, base)
