"""Convert rich Python types to chalk.arrow.v1 proto representation."""

from __future__ import annotations

import collections.abc
import dataclasses
import decimal
import enum
import ipaddress
import typing
import uuid
from datetime import date, datetime, time, timedelta
from typing import (
    Annotated,
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Set,
    Tuple,
    Union,
    get_args,
    get_origin,
    is_typeddict,
)


try:
    from pydantic import BaseModel as _PydanticBaseModel  # ty: ignore[unresolved-import]
except ImportError:
    _PydanticBaseModel = None  # type: ignore[assignment]

try:
    from types import UnionType
except ImportError:
    UnionType: Any = Union

_NoneType = type(None)
_AnnotatedType = type(Annotated[int, ""])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _unwrap_annotated(typ: Any) -> Any:
    if type(typ) is _AnnotatedType and hasattr(typ, "__metadata__"):
        return get_args(typ)[0]
    return typ


def _is_optional(typ: Any) -> bool:
    args = get_args(typ)
    return (
        get_origin(typ) in (Union, UnionType)
        and len(args) == 2
        and any(a is _NoneType for a in args)
    )


def _unwrap_optional(typ: Any) -> tuple[Any, bool]:
    """Return (inner_type, is_nullable)."""
    typ = _unwrap_annotated(typ)
    if _is_optional(typ):
        inner = next(a for a in get_args(typ) if a is not _NoneType)
        return inner, True
    return typ, False


def _is_namedtuple(value: Any) -> bool:
    return all(
        hasattr(value, attr) for attr in ("_fields", "_field_defaults", "_replace")
    )


def _has_attrs(typ: Any) -> bool:
    return hasattr(typ, "__attrs_attrs__")


def _is_pydantic_basemodel(typ: Any) -> bool:
    return (
        _PydanticBaseModel is not None
        and isinstance(typ, type)
        and issubclass(typ, _PydanticBaseModel)
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def rich_to_arrow_schema(
    params: dict[str, type],
) -> Any:
    """Convert a dict of {name: python_type} to an arrow_pb2.Schema.

    Each entry becomes a column/field in the schema.
    """
    from chalkcompute._gen.chalk.arrow.v1 import arrow_pb2

    columns = []
    for name, typ in params.items():
        inner, nullable = _unwrap_optional(typ)
        columns.append(
            arrow_pb2.Field(
                name=name,
                arrow_type=_rich_to_arrow_type(inner),
                nullable=nullable,
            )
        )
    return arrow_pb2.Schema(columns=columns)


def _rich_to_arrow_type(python_type: Any) -> Any:
    """Recursively convert a Python type to an arrow_pb2.ArrowType."""
    from chalkcompute._gen.chalk.arrow.v1 import arrow_pb2

    E = arrow_pb2.EmptyMessage

    python_type = _unwrap_annotated(python_type)
    if _is_optional(python_type):
        python_type = next(a for a in get_args(python_type) if a is not _NoneType)

    origin = get_origin(python_type)

    # ---- Generic / parameterised types ----
    if origin is not None:
        args = get_args(python_type)

        if origin in (Annotated, getattr(typing, "Annotated", Annotated)):
            return _rich_to_arrow_type(args[0])

        if origin in (list, List, set, Set, frozenset, FrozenSet):
            inner, nullable = _unwrap_optional(args[0])
            return arrow_pb2.ArrowType(
                large_list=arrow_pb2.List(
                    field_type=arrow_pb2.Field(
                        name="item",
                        arrow_type=_rich_to_arrow_type(inner),
                        nullable=nullable,
                    )
                )
            )

        if origin in (tuple, Tuple):
            if len(args) == 2 and args[1] is ...:
                inner, nullable = _unwrap_optional(args[0])
                return arrow_pb2.ArrowType(
                    large_list=arrow_pb2.List(
                        field_type=arrow_pb2.Field(
                            name="item",
                            arrow_type=_rich_to_arrow_type(inner),
                            nullable=nullable,
                        )
                    )
                )
            raise TypeError(
                "Only variable-length homogeneous tuples are supported (e.g. Tuple[int, ...])."
            )

        if origin in (dict, Dict, Mapping, collections.abc.Mapping):
            if len(args) != 2:
                raise TypeError("Dict types must have key and value annotations.")
            key_type, value_type = args
            val_inner, val_nullable = _unwrap_optional(value_type)
            return arrow_pb2.ArrowType(
                map=arrow_pb2.Map(
                    key_field=arrow_pb2.Field(
                        name="key",
                        arrow_type=_rich_to_arrow_type(key_type),
                        nullable=False,
                    ),
                    item_field=arrow_pb2.Field(
                        name="value",
                        arrow_type=_rich_to_arrow_type(val_inner),
                        nullable=val_nullable,
                    ),
                )
            )

        raise TypeError(f"Unsupported generic type: {origin}")

    if not isinstance(python_type, type):
        raise TypeError(f"Type annotations must be a type, got {python_type!r}.")

    # ---- Enums → convert via value type ----
    if issubclass(python_type, enum.Enum):
        # Infer from first member's value type
        first = next(iter(python_type))
        return _rich_to_arrow_type(type(first.value))

    # ---- Structured types (dataclass, namedtuple, TypedDict, attrs, pydantic) ----
    if (
        dataclasses.is_dataclass(python_type)
        or _is_namedtuple(python_type)
        or is_typeddict(python_type)
        or _has_attrs(python_type)
        or _is_pydantic_basemodel(python_type)
    ):
        annotations = typing.get_type_hints(python_type)
        sub_fields = []
        for field_name, field_type in annotations.items():
            if field_name.startswith("__"):
                continue
            inner, nullable = _unwrap_optional(field_type)
            sub_fields.append(
                arrow_pb2.Field(
                    name=field_name,
                    arrow_type=_rich_to_arrow_type(inner),
                    nullable=nullable,
                )
            )
        return arrow_pb2.ArrowType(struct=arrow_pb2.Struct(sub_field_types=sub_fields))

    # ---- Scalar / primitive types ----
    if issubclass(python_type, type(None)):
        return arrow_pb2.ArrowType(none=E())
    if issubclass(python_type, bool):
        return arrow_pb2.ArrowType(bool=E())
    if issubclass(python_type, int):
        return arrow_pb2.ArrowType(int64=E())
    if issubclass(python_type, float):
        return arrow_pb2.ArrowType(float64=E())
    if issubclass(python_type, str):
        return arrow_pb2.ArrowType(large_utf8=E())
    if issubclass(python_type, datetime):
        return arrow_pb2.ArrowType(
            timestamp=arrow_pb2.Timestamp(
                time_unit=arrow_pb2.TIME_UNIT_MICROSECOND,
                timezone="UTC",
            )
        )
    if issubclass(python_type, date):
        return arrow_pb2.ArrowType(date64=E())
    if issubclass(python_type, time):
        return arrow_pb2.ArrowType(time64=arrow_pb2.TIME_UNIT_MICROSECOND)
    if issubclass(python_type, timedelta):
        return arrow_pb2.ArrowType(duration=arrow_pb2.TIME_UNIT_MICROSECOND)
    if issubclass(python_type, bytes):
        return arrow_pb2.ArrowType(large_binary=E())
    if issubclass(python_type, decimal.Decimal):
        return arrow_pb2.ArrowType(large_utf8=E())
    if issubclass(python_type, uuid.UUID):
        return arrow_pb2.ArrowType(large_utf8=E())
    if issubclass(python_type, ipaddress.IPv4Address):
        return arrow_pb2.ArrowType(uint32=E())
    if issubclass(python_type, ipaddress.IPv6Address):
        return arrow_pb2.ArrowType(large_utf8=E())

    raise TypeError(f"Unable to convert type `{python_type}` to Arrow proto.")
