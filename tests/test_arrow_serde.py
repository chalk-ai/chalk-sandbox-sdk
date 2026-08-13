from __future__ import annotations

from typing import Annotated

import pyarrow as pa
import pytest

from chalkcompute._arrow_serde import (
    chalk_arrow_type,
    deserialize_scalar,
    has_chalk_arrow_contract,
    serialize_value,
)


class Point:
    __chalk_arrow_type__ = pa.struct(
        [
            pa.field("x", pa.int32(), nullable=False),
            pa.field("y", pa.int32(), nullable=False),
        ]
    )

    def __init__(self, x: int, y: int):
        self.x = x
        self.y = y

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Point) and (self.x, self.y) == (other.x, other.y)

    def __chalk_serialize__(self) -> pa.Scalar:
        return pa.scalar(
            {"x": self.x, "y": self.y},
            type=self.__chalk_arrow_type__,
        )

    @classmethod
    def __chalk_deserialize__(cls, value: pa.Scalar) -> Point:
        data = value.as_py()
        return cls(data["x"], data["y"])


def test_round_trip_uses_arrow_scalar():
    serialized = serialize_value(Point(2, 3), Point)

    assert isinstance(serialized, pa.StructScalar)
    assert serialized.type.equals(Point.__chalk_arrow_type__)
    assert deserialize_scalar(serialized, Point) == Point(2, 3)


def test_contract_requires_all_three_dunders():
    class ArrowTypeOnly:
        __chalk_arrow_type__ = pa.int64()

    class MethodsOnly:
        def __chalk_serialize__(self) -> pa.Scalar:
            return pa.scalar(1)

        @classmethod
        def __chalk_deserialize__(cls, value: pa.Scalar) -> MethodsOnly:
            del value
            return cls()

    assert has_chalk_arrow_contract(Point)
    assert not has_chalk_arrow_contract(ArrowTypeOnly)
    assert not has_chalk_arrow_contract(MethodsOnly)
    assert not has_chalk_arrow_contract(Point | None)
    assert not has_chalk_arrow_contract(Annotated[Point, "metadata"])


def test_partial_contract_is_not_custom_serialized():
    class Partial:
        __chalk_arrow_type__ = pa.int64()

    value = Partial()

    assert serialize_value(value, Partial) is value


def test_contract_is_not_inferred_from_runtime_value():
    value = Point(2, 3)

    assert serialize_value(value, None) is value


def test_serializer_must_return_arrow_scalar():
    class Invalid:
        __chalk_arrow_type__ = pa.int64()

        def __chalk_serialize__(self):
            return 1

        @classmethod
        def __chalk_deserialize__(cls, value: pa.Scalar) -> Invalid:
            del value
            return cls()

    with pytest.raises(TypeError, match="must return a pyarrow.Scalar"):
        serialize_value(Invalid(), Invalid)


def test_serializer_scalar_type_must_match_contract():
    class Invalid:
        __chalk_arrow_type__ = pa.int32()

        def __chalk_serialize__(self) -> pa.Scalar:
            return pa.scalar(1, type=pa.int64())

        @classmethod
        def __chalk_deserialize__(cls, value: pa.Scalar) -> Invalid:
            del value
            return cls()

    with pytest.raises(TypeError, match="returned Arrow type int64, expected int32"):
        serialize_value(Invalid(), Invalid)


def test_deserializer_must_be_classmethod():
    class Invalid:
        __chalk_arrow_type__ = pa.int64()

        def __chalk_serialize__(self) -> pa.Scalar:
            return pa.scalar(1)

        def __chalk_deserialize__(self, value: pa.Scalar) -> Invalid:
            del value
            return self

    with pytest.raises(TypeError, match="must be a classmethod"):
        chalk_arrow_type(Invalid)
