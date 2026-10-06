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
    from chalkcompute._arrow_serde import (
        chalk_arrow_type,
        has_chalk_arrow_contract,
    )

    columns = []
    for name, typ in params.items():
        # Unwrap Optional first: an Arrow-contract class is still one when it is
        # optional, and a column Chalk may bind to null must say so. The runtime
        # decoders already yield None for an invalid scalar.
        inner, nullable = _unwrap_optional(typ)
        if has_chalk_arrow_contract(inner):
            columns.append(
                arrow_pb.Field(
                    name=name,
                    arrow_type=chalk_arrow_type(inner),
                    nullable=nullable,
                    metadata=(
                        {"chalk.python.type": inner.__chalk_type_id__}
                        if isinstance(getattr(inner, "__chalk_type_id__", None), str)
                        else {}
                    ),
                )
            )
            continue
        columns.append(
            arrow_pb.Field(
                name=name,
                arrow_type=_rich_to_arrow_type(inner),
                nullable=nullable,
            )
        )
    return arrow_pb.Schema(columns=columns)


def _rich_to_arrow_type(
    python_type: Any, *, allow_custom_contract: bool = False
) -> Any:
    """Recursively convert a Python type to an ``arrow_pb.ArrowType``.

    Parameters
    ----------
    python_type
        The Python type annotation to convert.
    allow_custom_contract
        Whether a nested type may use its declared Arrow contract.

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

    if allow_custom_contract and has_chalk_arrow_contract(python_type):
        return chalk_arrow_type(python_type)

    origin = get_origin(python_type)

    # ---- Generic / parameterised types ----
    if origin is not None:
        args = get_args(python_type)

        if origin in (Annotated, getattr(typing, "Annotated", Annotated)):
            return _rich_to_arrow_type(args[0])

        if origin in (list, List, set, Set, frozenset, FrozenSet):
            inner, nullable = _unwrap_optional(args[0])
            return arrow_type(
                "large_list",
                arrow_pb.List(
                    field_type=arrow_pb.Field(
                        name="item",
                        arrow_type=_rich_to_arrow_type(
                            inner,
                            allow_custom_contract=not nullable
                            and origin in (list, List),
                        ),
                        nullable=nullable,
                    )
                ),
            )

        if origin in (tuple, Tuple):
            if len(args) == 2 and args[1] is ...:
                inner, nullable = _unwrap_optional(args[0])
                return arrow_type(
                    "large_list",
                    arrow_pb.List(
                        field_type=arrow_pb.Field(
                            name="item",
                            arrow_type=_rich_to_arrow_type(inner),
                            nullable=nullable,
                        )
                    ),
                )
            raise TypeError(
                "Only variable-length homogeneous tuples are supported (e.g. Tuple[int, ...])."
            )

        if origin in (dict, Dict, Mapping, collections.abc.Mapping):
            if len(args) != 2:
                raise TypeError("Dict types must have key and value annotations.")
            key_type, value_type = args
            val_inner, val_nullable = _unwrap_optional(value_type)
            return arrow_type(
                "map",
                arrow_pb.Map(
                    key_field=arrow_pb.Field(
                        name="key",
                        arrow_type=_rich_to_arrow_type(key_type),
                        nullable=False,
                    ),
                    item_field=arrow_pb.Field(
                        name="value",
                        arrow_type=_rich_to_arrow_type(val_inner),
                        nullable=val_nullable,
                    ),
                ),
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
                arrow_pb.Field(
                    name=field_name,
                    arrow_type=_rich_to_arrow_type(inner),
                    nullable=nullable,
                )
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
