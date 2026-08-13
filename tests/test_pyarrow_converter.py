"""Unit tests for chalkcompute._arrow_schema and chalkcompute._function._build_schemas."""

import dataclasses
import decimal
import enum
import ipaddress
import typing
import uuid
from datetime import date, datetime, time, timedelta

import attrs
import pyarrow as pa
import pytest

from chalkcompute._gen.chalk.arrow.v1 import arrow_pb2
from chalkcompute._arrow_schema import rich_to_arrow_schema


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Address:
    street: str
    zip_code: int


@attrs.define
class Person:
    name: str
    age: int


class Color(enum.Enum):
    RED = "red"
    GREEN = "green"
    BLUE = "blue"


class ArrowPoint:
    __chalk_arrow_type__ = arrow_pb2.ArrowType(
        struct=arrow_pb2.Struct(
            sub_field_types=[
                arrow_pb2.Field(
                    name="x",
                    arrow_type=arrow_pb2.ArrowType(int32=arrow_pb2.EmptyMessage()),
                    nullable=False,
                ),
                arrow_pb2.Field(
                    name="label",
                    arrow_type=arrow_pb2.ArrowType(utf8=arrow_pb2.EmptyMessage()),
                    nullable=True,
                ),
            ]
        )
    )

    def __chalk_serialize__(self) -> pa.Scalar:
        return pa.scalar(
            {"x": 1, "label": "point"},
            type=pa.struct(
                [
                    pa.field("x", pa.int32(), nullable=False),
                    pa.field("label", pa.string()),
                ]
            ),
        )

    @classmethod
    def __chalk_deserialize__(cls, value: pa.Scalar) -> "ArrowPoint":
        del value
        return cls()


# ---------------------------------------------------------------------------
# rich_to_arrow_schema: Python types → arrow_pb2.Schema
# ---------------------------------------------------------------------------


class TestRichToArrowSchema:
    def test_scalar_types(self):
        proto = rich_to_arrow_schema({"x": int, "y": float, "s": str, "b": bool})
        assert len(proto.columns) == 4
        assert proto.columns[0].name == "x"
        assert proto.columns[0].arrow_type.HasField("int64")
        assert proto.columns[1].arrow_type.HasField("float64")
        assert proto.columns[2].arrow_type.HasField("large_utf8")
        assert proto.columns[3].arrow_type.HasField("bool")

    def test_temporal_types(self):
        proto = rich_to_arrow_schema(
            {
                "ts": datetime,
                "d": date,
                "t": time,
                "dur": timedelta,
            }
        )
        assert proto.columns[0].arrow_type.HasField("timestamp")
        assert proto.columns[0].arrow_type.timestamp.timezone == "UTC"
        assert proto.columns[1].arrow_type.HasField("date64")
        assert proto.columns[2].arrow_type.HasField("time64")
        assert proto.columns[3].arrow_type.HasField("duration")

    def test_binary_types(self):
        proto = rich_to_arrow_schema({"raw": bytes})
        assert proto.columns[0].arrow_type.HasField("large_binary")

    def test_optional_nullable(self):
        proto = rich_to_arrow_schema(
            {
                "required": int,
                "optional": typing.Optional[int],
            }
        )
        assert proto.columns[0].nullable is False
        assert proto.columns[0].arrow_type.HasField("int64")
        assert proto.columns[1].nullable is True
        assert proto.columns[1].arrow_type.HasField("int64")

    def test_list_type(self):
        proto = rich_to_arrow_schema({"items": typing.List[int]})
        assert proto.columns[0].arrow_type.HasField("large_list")
        inner = proto.columns[0].arrow_type.large_list.field_type
        assert inner.arrow_type.HasField("int64")

    def test_dict_type(self):
        proto = rich_to_arrow_schema({"m": typing.Dict[str, float]})
        assert proto.columns[0].arrow_type.HasField("map")
        m = proto.columns[0].arrow_type.map
        assert m.key_field.arrow_type.HasField("large_utf8")
        assert m.item_field.arrow_type.HasField("float64")

    def test_dataclass_struct(self):
        proto = rich_to_arrow_schema({"addr": Address})
        assert proto.columns[0].arrow_type.HasField("struct")
        fields = proto.columns[0].arrow_type.struct.sub_field_types
        assert len(fields) == 2
        assert fields[0].name == "street"
        assert fields[0].arrow_type.HasField("large_utf8")
        assert fields[1].name == "zip_code"
        assert fields[1].arrow_type.HasField("int64")

    def test_attrs_struct(self):
        proto = rich_to_arrow_schema({"person": Person})
        assert proto.columns[0].arrow_type.HasField("struct")
        fields = proto.columns[0].arrow_type.struct.sub_field_types
        assert len(fields) == 2
        assert fields[0].name == "name"
        assert fields[0].arrow_type.HasField("large_utf8")
        assert fields[1].name == "age"
        assert fields[1].arrow_type.HasField("int64")

    def test_enum_type(self):
        proto = rich_to_arrow_schema({"color": Color})
        assert proto.columns[0].arrow_type.HasField("large_utf8")

    def test_special_types(self):
        proto = rich_to_arrow_schema(
            {
                "d": decimal.Decimal,
                "u": uuid.UUID,
                "ip4": ipaddress.IPv4Address,
                "ip6": ipaddress.IPv6Address,
            }
        )
        assert proto.columns[0].arrow_type.HasField("large_utf8")  # Decimal
        assert proto.columns[1].arrow_type.HasField("large_utf8")  # UUID
        assert proto.columns[2].arrow_type.HasField("uint32")  # IPv4
        assert proto.columns[3].arrow_type.HasField("large_utf8")  # IPv6

    def test_custom_type_uses_literal_arrow_proto(self):
        proto = rich_to_arrow_schema({"point": ArrowPoint})

        arrow_type = proto.columns[0].arrow_type
        assert arrow_type.HasField("struct")
        fields = arrow_type.struct.sub_field_types
        assert [field.name for field in fields] == ["x", "label"]
        assert fields[0].arrow_type.HasField("int32")
        assert fields[0].nullable is False
        assert fields[1].arrow_type.HasField("utf8")
        assert fields[1].nullable is True

    @pytest.mark.parametrize(
        "annotation",
        [typing.Optional[ArrowPoint], typing.Annotated[ArrowPoint, "metadata"]],
    )
    def test_custom_type_must_be_direct_annotation(self, annotation):
        with pytest.raises(TypeError, match="Unable to convert type"):
            rich_to_arrow_schema({"point": annotation})

    def test_custom_type_requires_arrow_proto(self):
        class Invalid:
            __chalk_arrow_type__ = "struct<x: int64>"

            def __chalk_serialize__(self) -> pa.Scalar:
                raise NotImplementedError

            @classmethod
            def __chalk_deserialize__(cls, value: pa.Scalar) -> "Invalid":
                raise NotImplementedError

        with pytest.raises(TypeError):
            rich_to_arrow_schema({"value": Invalid})


# ---------------------------------------------------------------------------
# _build_schemas: function signature → input/output protos
# ---------------------------------------------------------------------------


class TestBuildSchemas:
    def test_simple_function(self):
        from chalkcompute._function import _build_schemas

        def add(x: int, y: int) -> int:
            return x + y

        input_proto, output_proto = _build_schemas(add)
        assert len(input_proto.columns) == 2
        assert input_proto.columns[0].name == "x"
        assert input_proto.columns[0].arrow_type.HasField("int64")
        assert input_proto.columns[1].name == "y"
        assert output_proto.columns[0].name == "result"
        assert output_proto.columns[0].arrow_type.HasField("int64")

    def test_mixed_types(self):
        from chalkcompute._function import _build_schemas

        def greet(name: str, count: int) -> str:
            return name * count

        input_proto, output_proto = _build_schemas(greet)
        assert input_proto.columns[0].arrow_type.HasField("large_utf8")
        assert input_proto.columns[1].arrow_type.HasField("int64")
        assert output_proto.columns[0].arrow_type.HasField("large_utf8")
