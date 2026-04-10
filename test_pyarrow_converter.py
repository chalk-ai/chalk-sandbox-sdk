"""Tests for chalkcompute.pyarrow_converter.rich_to_pyarrow."""

import dataclasses
import decimal
import enum
import ipaddress
import typing
import uuid
from datetime import date, datetime, time, timedelta

import pyarrow as pa
import pytest

from chalkcompute.pyarrow_converter import rich_to_pyarrow


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Address:
    street: str
    zip_code: int


class Color(enum.Enum):
    RED = "red"
    GREEN = "green"
    BLUE = "blue"


# ---------------------------------------------------------------------------
# Scalar types
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "python_type, expected",
    [
        (str, pa.large_utf8()),
        (int, pa.int64()),
        (float, pa.float64()),
        (bool, pa.bool_()),
        (bytes, pa.large_binary()),
        (datetime, pa.timestamp("us", "UTC")),
        (date, pa.date64()),
        (time, pa.time64("us")),
        (timedelta, pa.duration("us")),
        (decimal.Decimal, pa.large_utf8()),
        (uuid.UUID, pa.large_utf8()),
        (ipaddress.IPv4Address, pa.uint32()),
        (ipaddress.IPv6Address, pa.large_utf8()),
    ],
)
def test_scalar_types(python_type, expected):
    assert rich_to_pyarrow(python_type) == expected


def test_date_in_struct():
    assert rich_to_pyarrow(date, in_struct=True) == pa.date32()


# ---------------------------------------------------------------------------
# Container types
# ---------------------------------------------------------------------------


def test_list_int():
    assert rich_to_pyarrow(typing.List[int]) == pa.large_list(pa.int64())


def test_dict_str_float():
    result = rich_to_pyarrow(typing.Dict[str, float])
    assert isinstance(result, pa.MapType)


def test_tuple_str_ellipsis():
    assert rich_to_pyarrow(typing.Tuple[str, ...]) == pa.large_list(pa.large_utf8())


# ---------------------------------------------------------------------------
# Optional
# ---------------------------------------------------------------------------


def test_optional_int():
    assert rich_to_pyarrow(typing.Optional[int]) == pa.int64()


# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------


def test_enum():
    assert rich_to_pyarrow(Color) == pa.large_utf8()


# ---------------------------------------------------------------------------
# Dataclass / struct
# ---------------------------------------------------------------------------


def test_dataclass():
    result = rich_to_pyarrow(Address)
    assert isinstance(result, pa.StructType)
    assert result.num_fields == 2
    assert result.field("street").type == pa.large_utf8()
    assert result.field("zip_code").type == pa.int64()
