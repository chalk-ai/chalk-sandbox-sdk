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
from protobuf import Oneof
from typing import (
    Annotated,
    Any,
    Dict,
    Mapping,
    Union,
    get_args,
    get_origin,
    is_typeddict,
)


_PydanticBaseModel: Any = None
try:
    from pydantic import BaseModel as _PydanticBaseModel  # noqa: F811
except ImportError:
    pass

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
    """Return the inner type and whether the original type is nullable.

    Parameters
    ----------
    typ
        The type annotation to unwrap.

    Returns
    -------
    tuple[Any, bool]
        A tuple of ``(inner_type, is_nullable)``.
    """
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
    params: dict[str, Any],
) -> Any:
    """Convert a mapping of ``{name: python_type}`` to an ``arrow_pb.Schema``.

    Each entry becomes a column/field in the schema.

    Parameters
    ----------
    params
        Mapping of column name to Python type annotation.

    Returns
    -------
    arrow_pb.Schema
        The serialized Arrow schema.
    """
    from chalkcompute._gen.chalk.arrow.v1 import arrow_pb
    from chalkcompute._arrow_serde import has_typed_wire_annotation

    return arrow_pb.Schema(
        columns=[
            _rich_field(name, typ, typed_wire=has_typed_wire_annotation(typ))
            for name, typ in params.items()
        ]
    )


def _rich_field(name: str, annotation: Any, *, typed_wire: bool) -> Any:
    from chalkcompute._arrow_runtime import _chalk_record_kind
    from chalkcompute._arrow_serde import has_chalk_arrow_contract
    from chalkcompute._gen.chalk.arrow.v1 import arrow_pb

    inner, nullable = _unwrap_optional(annotation)
    inner = _unwrap_annotated(inner)
    origin = get_origin(inner)
    metadata: dict[str, str] = {}
    if (record := _chalk_record_kind(inner)) is not None:
        metadata["chalk.python.record"] = record
        metadata["chalk.python.type"] = f"{inner.__module__}.{inner.__qualname__}"
    elif origin in (set, frozenset, tuple):
        metadata["chalk.python.collection"] = origin.__name__
    elif has_chalk_arrow_contract(inner) and isinstance(
        getattr(inner, "__chalk_type_id__", None), str
    ):
        metadata["chalk.python.type"] = inner.__chalk_type_id__
    elif (
        typed_wire
        and isinstance(inner, type)
        and (
            inner
            not in (
                bool,
                int,
                float,
                str,
                bytes,
                type(None),
                datetime,
                date,
                time,
                timedelta,
            )
            and not is_typeddict(inner)
        )
    ):
        # A compiler with only the physical type cannot reproduce Python construction,
        # equality, or hashing for these collection members.
        metadata["chalk.python.type"] = f"{inner.__module__}.{inner.__qualname__}"
    return arrow_pb.Field(
        name=name,
        arrow_type=_rich_to_arrow_type(inner, typed_wire=typed_wire),
        nullable=nullable,
        metadata=metadata,
    )


def _rich_to_arrow_type(python_type: Any, *, typed_wire: bool) -> Any:
    """Recursively convert a Python type to an ``arrow_pb.ArrowType``.

    Parameters
    ----------
    python_type
        The Python type annotation to convert.

    Returns
    -------
    arrow_pb.ArrowType
        The Arrow type equivalent.

    Raises
    ------
    TypeError
        If the Python type cannot be represented as an Arrow type.
    """
    from chalkcompute._gen.chalk.arrow.v1 import arrow_pb
    from chalkcompute._arrow_serde import (
        chalk_arrow_type,
        has_chalk_arrow_contract,
    )

    E = arrow_pb.EmptyMessage

    def arrow_type(field: str, value: Any) -> arrow_pb.ArrowType:
        return arrow_pb.ArrowType(
            arrow_type_enum=Oneof(field, value)  # ty: ignore[invalid-argument-type]
        )

    python_type = _unwrap_annotated(python_type)
    if _is_optional(python_type):
        python_type = next(a for a in get_args(python_type) if a is not _NoneType)

    if has_chalk_arrow_contract(python_type):
        return chalk_arrow_type(python_type)

    origin = get_origin(python_type)

    # ---- Generic / parameterised types ----
    if origin is not None:
        args = get_args(python_type)

        if origin in (Annotated, getattr(typing, "Annotated", Annotated)):
            return _rich_to_arrow_type(args[0], typed_wire=typed_wire)

        if origin in (list, set, frozenset) or (
            origin is tuple and len(args) == 2 and args[1] is ...
        ):
            if not args:
                raise TypeError(f"{origin.__name__} requires an element annotation")
            return arrow_type(
                "large_list",
                arrow_pb.List(
                    field_type=_rich_field("item", args[0], typed_wire=typed_wire)
                ),
            )

        if origin is tuple:
            if not args and python_type is typing.Tuple:
                raise TypeError("Tuple requires element annotations")
            return arrow_type(
                "struct",
                arrow_pb.Struct(
                    sub_field_types=[
                        _rich_field(str(i), item, typed_wire=typed_wire)
                        for i, item in enumerate(args)
                    ]
                ),
            )

        if origin in (dict, Dict, Mapping, collections.abc.Mapping):
            if len(args) != 2:
                raise TypeError("Dict types must have key and value annotations.")
            key_type, value_type = args
            key_field = _rich_field("key", key_type, typed_wire=typed_wire)
            key_field.nullable = False
            return arrow_type(
                "map",
                arrow_pb.Map(
                    key_field=key_field,
                    item_field=_rich_field("value", value_type, typed_wire=typed_wire),
                ),
            )

        raise TypeError(f"Unsupported generic type: {origin}")

    if not isinstance(python_type, type):
        raise TypeError(f"Type annotations must be a type, got {python_type!r}.")

    # ---- Enums → convert via value type ----
    if issubclass(python_type, enum.Enum):
        # Infer from first member's value type
        first = next(iter(python_type))
        return _rich_to_arrow_type(type(first.value), typed_wire=typed_wire)

    # ---- Structured types (dataclass, namedtuple, TypedDict, attrs, pydantic) ----
    if (
        dataclasses.is_dataclass(python_type)
        or _is_namedtuple(python_type)
        or is_typeddict(python_type)
        or _has_attrs(python_type)
        or _is_pydantic_basemodel(python_type)
    ):
        from chalkcompute._arrow_runtime import _chalk_struct_fields

        annotations = _chalk_struct_fields(python_type)
        sub_fields = []
        for field_name, field_type in annotations.items():
            if field_name.startswith("__"):
                continue
            sub_fields.append(
                _rich_field(field_name, field_type, typed_wire=typed_wire)
            )
        return arrow_type("struct", arrow_pb.Struct(sub_field_types=sub_fields))

    # ---- Scalar / primitive types ----
    if issubclass(python_type, type(None)):
        return arrow_type("none", E())
    if issubclass(python_type, bool):
        return arrow_type("bool", E())
    if issubclass(python_type, int):
        return arrow_type("int64", E())
    if issubclass(python_type, float):
        return arrow_type("float64", E())
    if issubclass(python_type, str):
        return arrow_type("large_utf8", E())
    if issubclass(python_type, datetime):
        return arrow_type(
            "timestamp",
            arrow_pb.Timestamp(
                time_unit=arrow_pb.TimeUnit.MICROSECOND,
                timezone="UTC",
            ),
        )
    if issubclass(python_type, date):
        return arrow_type("date64", E())
    if issubclass(python_type, time):
        return arrow_type("time64", arrow_pb.TimeUnit.MICROSECOND)
    if issubclass(python_type, timedelta):
        return arrow_type("duration", arrow_pb.TimeUnit.MICROSECOND)
    if issubclass(python_type, bytes):
        return arrow_type("large_binary", E())
    if issubclass(python_type, decimal.Decimal):
        return arrow_type("large_utf8", E())
    if issubclass(python_type, uuid.UUID):
        return arrow_type("large_utf8", E())
    if issubclass(python_type, ipaddress.IPv4Address):
        return arrow_type("uint32", E())
    if issubclass(python_type, ipaddress.IPv6Address):
        return arrow_type("large_utf8", E())

    raise TypeError(f"Unable to convert type `{python_type}` to Arrow proto.")
