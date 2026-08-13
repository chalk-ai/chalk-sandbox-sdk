from __future__ import annotations

import pyarrow as pa
import pytest

from chalkcompute._arrow_serde import (
    chalk_arrow_type,
    deserialize_scalar,
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


def test_optional_none_bypasses_dunders():
    null = pa.scalar(None, type=Point.__chalk_arrow_type__)

    serialized = serialize_value(None, Point | None)
    assert isinstance(serialized, pa.StructScalar)
    assert serialized.type.equals(Point.__chalk_arrow_type__)
    assert not serialized.is_valid
    assert deserialize_scalar(null, Point | None) is None


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
