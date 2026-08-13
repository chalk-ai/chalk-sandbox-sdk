from __future__ import annotations

from typing import Annotated

import pyarrow as pa

from chalkcompute._gen.chalk.arrow.v1 import arrow_pb2
from chalkcompute._arrow_serde import (
    chalk_arrow_type,
    has_chalk_arrow_contract,
)


class Value:
    __chalk_arrow_type__ = arrow_pb2.ArrowType(int64=arrow_pb2.EmptyMessage())

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
        __chalk_arrow_type__ = arrow_pb2.ArrowType(int64=arrow_pb2.EmptyMessage())

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
