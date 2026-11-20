import typing

import pyarrow as pa
import pytest

from chalkcompute._arrow_schema import rich_to_arrow_schema
from chalkcompute._arrow_serde import (
    arrow_decoder_for_annotation,
    arrow_encoder_for_annotation,
    handler_runtime_source,
)


def test_collection_field_identity_and_tuple_positions():
    field, ordinary = rich_to_arrow_schema(
        {
            "pairs": list[tuple[frozenset[int | None], tuple[str, ...] | None]],
            "plain": list[int],
        }
    ).columns
    assert not field.metadata
    assert not ordinary.metadata
    pair = field.arrow_type.arrow_type_enum.value.field_type
    assert pair.metadata == {"chalk.python.collection": "tuple"}
    left, right = pair.arrow_type.arrow_type_enum.value.sub_field_types
    assert (left.name, left.metadata, left.nullable) == (
        "0",
        {"chalk.python.collection": "frozenset"},
        False,
    )
    assert left.arrow_type.arrow_type_enum.value.field_type.nullable
    assert (right.name, right.metadata, right.nullable) == (
        "1",
        {"chalk.python.collection": "tuple"},
        True,
    )
    assert right.arrow_type.has_field("large_list")


@pytest.mark.parametrize(
    ("annotation", "value"),
    [
        (set[str], {"a", "b"}),
        (set[str] | None, None),
        (set[str], set()),
        (tuple[int | None, ...], (None, 1, None, 1)),
        (tuple[int, str], (3, "four")),
        (tuple[()], ()),
        (list[tuple[int, frozenset[str]] | None], [None, (0, frozenset())]),
        (dict[tuple[int, str], set[str] | None], {(1, "x"): set(), (2, "y"): None}),
    ],
)
def test_local_and_generated_codecs_agree_across_ipc(annotation, value):
    namespace = {}
    exec(handler_runtime_source(), namespace)
    encoder = arrow_encoder_for_annotation(annotation)
    scalar = encoder(value)
    # Cross the real IPC boundary so empty or all-null values exercise field types.
    batch = pa.record_batch([pa.repeat(scalar, 1)], names=["x"])
    buffer = pa.BufferOutputStream()
    with pa.ipc.new_stream(buffer, batch.schema) as writer:
        writer.write_batch(batch)
    col = pa.ipc.open_stream(buffer.getvalue()).read_next_batch().column(0)
    (decoded,) = namespace["_chalk_column_decoder"](annotation)(col)
    assert type(decoded) is type(value)
    assert decoded == value
    encoded = namespace["_chalk_encoder"](annotation, dtype=scalar.type)(decoded)
    actual = arrow_decoder_for_annotation(annotation)(encoded)
    assert type(actual) is type(value)
    assert actual == value


def test_duplicate_wire_values_use_python_collection_semantics():
    raw = pa.array([["b", "a", "a"], [], None])
    namespace = {}
    exec(handler_runtime_source(), namespace)
    for annotation, expected in (
        (typing.Set[str], [{"a", "b"}, set(), None]),
        (typing.FrozenSet[str], [frozenset({"a", "b"}), frozenset(), None]),
        (typing.Tuple[str, ...], [("b", "a", "a"), (), None]),
        (list[str], [["b", "a", "a"], [], None]),
    ):
        got = namespace["_chalk_column_decoder"](annotation)(raw)
        assert got == expected
        assert type(got[0]) is type(expected[0])


def test_fixed_tuple_rejects_incorrect_arity():
    with pytest.raises(TypeError, match="fixed tuple of length 2"):
        arrow_encoder_for_annotation(tuple[int, str])((1,))


def test_named_collection_members_keep_python_type():
    import dataclasses
    import enum

    class Suit(enum.Enum):
        HEARTS = "hearts"

    @dataclasses.dataclass(frozen=True)
    class Hand:
        suits: frozenset[Suit]

    schema = rich_to_arrow_schema({"hand": Hand}).columns[0]
    (suits,) = schema.arrow_type.arrow_type_enum.value.sub_field_types
    suit = suits.arrow_type.arrow_type_enum.value.field_type
    assert schema.metadata["chalk.python.type"].endswith(".Hand")
    assert suits.metadata == {"chalk.python.collection": "frozenset"}
    assert suit.metadata["chalk.python.type"].endswith(".Suit")
    hand = Hand(frozenset({Suit.HEARTS}))
    scalar = arrow_encoder_for_annotation(Hand)(hand)
    assert scalar.as_py() == {"suits": ["hearts"]}
    assert arrow_decoder_for_annotation(Hand)(scalar) == hand
