from __future__ import annotations

from typing import Annotated

import pyarrow as pa
from protobuf import Oneof

from chalkcompute._gen.chalk.arrow.v1 import arrow_pb
from chalkcompute._arrow_serde import (
    arrow_decoder_for_annotation,
    arrow_encoder_for_annotation,
    chalk_arrow_type,
    has_chalk_arrow_contract,
)


class Value:
    __chalk_arrow_type__ = arrow_pb.ArrowType(
        arrow_type_enum=Oneof("int64", arrow_pb.EmptyMessage())
    )
    __chalk_pyarrow_type__ = staticmethod(pa.int64)

    def __init__(self, value: int):
        self.value = value

    def __chalk_serialize__(self) -> pa.Scalar:
        return pa.scalar(self.value, type=pa.int64())

    @classmethod
    def __chalk_deserialize__(cls, value: pa.Scalar) -> Value:
        return cls(value.as_py())


def test_arrow_type_is_used_directly():
    assert chalk_arrow_type(Value) is Value.__chalk_arrow_type__


def test_arrow_type_is_the_only_contract_marker():
    class ArrowTypeOnly:
        __chalk_arrow_type__ = arrow_pb.ArrowType(
            arrow_type_enum=Oneof("int64", arrow_pb.EmptyMessage())
        )

    class MethodsOnly:
        def __chalk_serialize__(self) -> pa.Scalar:
            return pa.scalar(1)

        @classmethod
        def __chalk_deserialize__(cls, value: pa.Scalar) -> MethodsOnly:
            del value
            return cls()

    assert has_chalk_arrow_contract(Value)
    assert has_chalk_arrow_contract(ArrowTypeOnly)
    assert not has_chalk_arrow_contract(MethodsOnly)
    assert not has_chalk_arrow_contract(Value | None)
    assert not has_chalk_arrow_contract(Annotated[Value, "metadata"])


def test_list_of_arrow_backed_values_roundtrips() -> None:
    encoder = arrow_encoder_for_annotation(list[Value])
    decoder = arrow_decoder_for_annotation(list[Value])
    assert encoder is not None
    assert decoder is not None

    encoded = encoder([Value(1), Value(2)])
    assert encoded.type.equals(
        pa.large_list(pa.field("item", pa.int64(), nullable=False))
    )
    assert [item.value for item in decoder(encoded)] == [1, 2]

    empty = encoder([])
    assert empty.as_py() == []
    assert empty.type.equals(
        pa.large_list(pa.field("item", pa.int64(), nullable=False))
    )
