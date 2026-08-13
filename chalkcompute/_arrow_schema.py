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
    """Convert a mapping of ``{name: python_type}`` to an ``arrow_pb2.Schema``.

    Each entry becomes a column/field in the schema.

    Parameters
    ----------
    params
        Mapping of column name to Python type annotation.

    Returns
    -------
    arrow_pb2.Schema
        The serialized Arrow schema.
    """
    from chalkcompute._gen.chalk.arrow.v1 import arrow_pb2
    from chalkcompute._arrow_serde import (
        chalk_arrow_type,
        has_chalk_arrow_contract,
    )

    columns = []
    for name, typ in params.items():
        if has_chalk_arrow_contract(typ):
            columns.append(
                arrow_pb2.Field(
                    name=name,
                    arrow_type=_pyarrow_to_arrow_type(chalk_arrow_type(typ)),
                    nullable=False,
                )
            )
            continue
        inner, nullable = _unwrap_optional(typ)
        columns.append(
            arrow_pb2.Field(
                name=name,
                arrow_type=_rich_to_arrow_type(inner),
                nullable=nullable,
            )
        )
    return arrow_pb2.Schema(columns=columns)


def _pyarrow_metadata(metadata: Any) -> dict[str, str]:
    """Convert Arrow's byte metadata to the string metadata used by the proto."""
    if not metadata:
        return {}
    try:
        return {
            key.decode("utf-8"): value.decode("utf-8")
            for key, value in metadata.items()
        }
    except UnicodeDecodeError as error:
        raise TypeError(
            "Arrow field metadata keys and values must be valid UTF-8."
        ) from error


def _pyarrow_time_unit(unit: str) -> Any:
    from chalkcompute._gen.chalk.arrow.v1 import arrow_pb2

    units = {
        "s": arrow_pb2.TIME_UNIT_SECOND,
        "ms": arrow_pb2.TIME_UNIT_MILLISECOND,
        "us": arrow_pb2.TIME_UNIT_MICROSECOND,
        "ns": arrow_pb2.TIME_UNIT_NANOSECOND,
    }
    try:
        return units[unit]
    except KeyError as error:
        raise TypeError(f"Unsupported PyArrow time unit: {unit!r}.") from error


def _pyarrow_field_to_proto(field: Any) -> Any:
    from chalkcompute._gen.chalk.arrow.v1 import arrow_pb2

    return arrow_pb2.Field(
        name=field.name,
        arrow_type=_pyarrow_to_arrow_type(field.type),
        nullable=field.nullable,
        metadata=_pyarrow_metadata(field.metadata),
    )


def _pyarrow_to_arrow_type(data_type: Any) -> Any:
    """Convert a concrete ``pyarrow.DataType`` to Chalk's Arrow type proto."""
    import pyarrow as pa

    from chalkcompute._gen.chalk.arrow.v1 import arrow_pb2

    if not isinstance(data_type, pa.DataType):
        raise TypeError(f"Expected a pyarrow.DataType, got {type(data_type).__name__}.")

    E = arrow_pb2.EmptyMessage

    extension_base = getattr(pa, "BaseExtensionType", None)
    if extension_base is not None and isinstance(data_type, extension_base):
        return arrow_pb2.ArrowType(
            extension=arrow_pb2.Extension(
                name=data_type.extension_name,
                storage_type=_pyarrow_to_arrow_type(data_type.storage_type),
            )
        )

    if pa.types.is_null(data_type):
        return arrow_pb2.ArrowType(none=E())
    if pa.types.is_boolean(data_type):
        return arrow_pb2.ArrowType(bool=E())
    if pa.types.is_float16(data_type):
        return arrow_pb2.ArrowType(float16=E())
    if pa.types.is_float32(data_type):
        return arrow_pb2.ArrowType(float32=E())
    if pa.types.is_float64(data_type):
        return arrow_pb2.ArrowType(float64=E())

    integer_fields = (
        (pa.types.is_int8, "int8"),
        (pa.types.is_int16, "int16"),
        (pa.types.is_int32, "int32"),
        (pa.types.is_int64, "int64"),
        (pa.types.is_uint8, "uint8"),
        (pa.types.is_uint16, "uint16"),
        (pa.types.is_uint32, "uint32"),
        (pa.types.is_uint64, "uint64"),
    )
    for predicate, field_name in integer_fields:
        if predicate(data_type):
            scalar_type_kwargs: Any = {field_name: E()}
            return arrow_pb2.ArrowType(**scalar_type_kwargs)

    if pa.types.is_string(data_type):
        return arrow_pb2.ArrowType(utf8=E())
    if pa.types.is_large_string(data_type):
        return arrow_pb2.ArrowType(large_utf8=E())
    if pa.types.is_fixed_size_binary(data_type):
        return arrow_pb2.ArrowType(fixed_size_binary=data_type.byte_width)
    if pa.types.is_binary(data_type):
        return arrow_pb2.ArrowType(binary=E())
    if pa.types.is_large_binary(data_type):
        return arrow_pb2.ArrowType(large_binary=E())

    if pa.types.is_timestamp(data_type):
        return arrow_pb2.ArrowType(
            timestamp=arrow_pb2.Timestamp(
                time_unit=_pyarrow_time_unit(data_type.unit),
                timezone=data_type.tz or "",
            )
        )
    if pa.types.is_date32(data_type):
        return arrow_pb2.ArrowType(date32=E())
    if pa.types.is_date64(data_type):
        return arrow_pb2.ArrowType(date64=E())
    if pa.types.is_time32(data_type):
        return arrow_pb2.ArrowType(time32=_pyarrow_time_unit(data_type.unit))
    if pa.types.is_time64(data_type):
        return arrow_pb2.ArrowType(time64=_pyarrow_time_unit(data_type.unit))
    if pa.types.is_duration(data_type):
        return arrow_pb2.ArrowType(duration=_pyarrow_time_unit(data_type.unit))

    if pa.types.is_decimal128(data_type):
        return arrow_pb2.ArrowType(
            decimal_128=arrow_pb2.Decimal(
                precision=data_type.precision,
                scale=data_type.scale,
            )
        )
    if pa.types.is_decimal256(data_type):
        return arrow_pb2.ArrowType(
            decimal_256=arrow_pb2.Decimal(
                precision=data_type.precision,
                scale=data_type.scale,
            )
        )

    if pa.types.is_list(data_type):
        return arrow_pb2.ArrowType(
            list=arrow_pb2.List(
                field_type=_pyarrow_field_to_proto(data_type.value_field)
            )
        )
    if pa.types.is_large_list(data_type):
        return arrow_pb2.ArrowType(
            large_list=arrow_pb2.List(
                field_type=_pyarrow_field_to_proto(data_type.value_field)
            )
        )
    if pa.types.is_fixed_size_list(data_type):
        return arrow_pb2.ArrowType(
            fixed_size_list=arrow_pb2.FixedSizeList(
                field_type=_pyarrow_field_to_proto(data_type.value_field),
                list_size=data_type.list_size,
            )
        )
    if pa.types.is_struct(data_type):
        return arrow_pb2.ArrowType(
            struct=arrow_pb2.Struct(
                sub_field_types=[_pyarrow_field_to_proto(field) for field in data_type]
            )
        )
    if pa.types.is_map(data_type):
        return arrow_pb2.ArrowType(
            map=arrow_pb2.Map(
                key_field=_pyarrow_field_to_proto(data_type.key_field),
                item_field=_pyarrow_field_to_proto(data_type.item_field),
                keys_sorted=data_type.keys_sorted,
            )
        )

    raise TypeError(f"Unable to convert PyArrow type `{data_type}` to Arrow proto.")


def _rich_to_arrow_type(python_type: Any) -> Any:
    """Recursively convert a Python type to an ``arrow_pb2.ArrowType``.

    Parameters
    ----------
    python_type
        The Python type annotation to convert.

    Returns
    -------
    arrow_pb2.ArrowType
        The Arrow type equivalent.

    Raises
    ------
    TypeError
        If the Python type cannot be represented as an Arrow type.
    """
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
