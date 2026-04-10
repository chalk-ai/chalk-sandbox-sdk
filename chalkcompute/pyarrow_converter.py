from __future__ import annotations

import collections.abc
import dataclasses
import decimal
import enum
import functools
import ipaddress
import typing
import uuid
from datetime import date, datetime, time, timedelta
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Dict,
    FrozenSet,
    List,
    Literal,
    Mapping,
    Set,
    Tuple,
    Type,
    Union,
    get_args,
    get_origin,
    is_typeddict,
)

if TYPE_CHECKING:
    import pyarrow as pa

try:
    from types import UnionType
except ImportError:
    UnionType = Union  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NoneType = type(None)
_AnnotatedType = type(Annotated[int, ""])


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


def _unwrap_optional_and_annotated(typ: Any) -> Any:
    typ = _unwrap_annotated(typ)
    if _is_optional(typ):
        return next(a for a in get_args(typ) if a is not _NoneType)
    return typ


def _is_namedtuple(value: Any) -> bool:
    return all(
        hasattr(value, attr) for attr in ("_fields", "_field_defaults", "_replace")
    )


def _is_pydantic_basemodel(typ: Any) -> bool:
    try:
        from pydantic import BaseModel as _PydanticBaseModel
    except ImportError:
        return False
    return isinstance(typ, type) and issubclass(typ, _PydanticBaseModel)


def _has_attrs(typ: Any) -> bool:
    try:
        import attrs

        return attrs.has(typ)
    except ImportError:
        return False


@functools.lru_cache(None)
def _get_enum_value_type(enum_cls: Type[enum.Enum]) -> type:
    typ = None
    for member in enum_cls:
        val_type = type(member.value)
        if typ is None:
            typ = val_type
            continue
        if issubclass(val_type, typ):
            continue
        if issubclass(val_type, int) and issubclass(typ, float):
            continue
        if issubclass(typ, int) and issubclass(val_type, float):
            typ = val_type
            continue
        if issubclass(typ, val_type):
            typ = val_type
            continue
        raise TypeError(
            f"Enum {enum_cls.__name__} has members of heterogeneous types "
            f"({typ.__name__} and {val_type.__name__}); all members must have the same type."
        )
    if typ is None:
        raise ValueError(f"Enum {enum_cls.__name__} has no members")
    return typ


def _convert_proto_descriptor_to_pyarrow(
    descriptor: Any,
) -> pa.DataType:
    """Best-effort protobuf message -> pa.struct conversion using the descriptor."""
    import pyarrow as pa
    from google.protobuf.descriptor import FieldDescriptor

    _PROTO_SCALAR: dict[int, pa.DataType] = {
        FieldDescriptor.TYPE_DOUBLE: pa.float64(),
        FieldDescriptor.TYPE_FLOAT: pa.float32(),
        FieldDescriptor.TYPE_INT64: pa.int64(),
        FieldDescriptor.TYPE_UINT64: pa.uint64(),
        FieldDescriptor.TYPE_INT32: pa.int32(),
        FieldDescriptor.TYPE_UINT32: pa.uint32(),
        FieldDescriptor.TYPE_FIXED64: pa.uint64(),
        FieldDescriptor.TYPE_FIXED32: pa.uint32(),
        FieldDescriptor.TYPE_SFIXED64: pa.int64(),
        FieldDescriptor.TYPE_SFIXED32: pa.int32(),
        FieldDescriptor.TYPE_SINT64: pa.int64(),
        FieldDescriptor.TYPE_SINT32: pa.int32(),
        FieldDescriptor.TYPE_BOOL: pa.bool_(),
        FieldDescriptor.TYPE_STRING: pa.large_utf8(),
        FieldDescriptor.TYPE_BYTES: pa.large_binary(),
        FieldDescriptor.TYPE_ENUM: pa.int32(),
    }

    fields: list[pa.Field] = []
    for field in descriptor.fields:
        if field.message_type is not None:
            inner = _convert_proto_descriptor_to_pyarrow(field.message_type)
        else:
            inner = _PROTO_SCALAR.get(field.type, pa.large_utf8())

        if field.label == FieldDescriptor.LABEL_REPEATED:
            inner = pa.large_list(inner)

        fields.append(pa.field(field.name, inner))

    return pa.struct(fields)


def _is_protobuf_message(typ: Any) -> bool:
    try:
        import google.protobuf.message

        return isinstance(typ, type) and issubclass(
            typ, google.protobuf.message.Message
        )
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Main conversion function
# ---------------------------------------------------------------------------


def rich_to_pyarrow(
    python_type: Type,
    name: str = "",
    *,
    in_struct: bool = False,
    respect_nullability: bool = False,
) -> pa.DataType:
    """Recursively convert a Python type to a PyArrow DataType.

    Parameters
    ----------
    python_type
        The Python type annotation to convert.
    name
        Field name, used in error messages and struct field naming.
    in_struct
        Whether we are inside a struct (affects e.g. date32 vs date64).
    respect_nullability
        When True, struct fields carry correct nullable metadata.
    """
    import pyarrow as pa

    python_type = _unwrap_optional_and_annotated(python_type)
    origin = get_origin(python_type)

    # ---- Generic / parameterised types ----
    if origin is not None:
        args = get_args(python_type)

        if origin in (Annotated, getattr(typing, "Annotated", Annotated)):
            if len(args) < 1:
                raise TypeError(
                    "Annotated types must contain the underlying type as the first argument."
                )
            return rich_to_pyarrow(
                args[0],
                name,
                in_struct=in_struct,
                respect_nullability=respect_nullability,
            )

        if origin in (Literal, getattr(typing, "Literal", Literal)):
            if len(args) < 1:
                raise TypeError("Literal types must contain at least one argument.")
            first_type = type(args[0])
            for idx, other_value in enumerate(args[1:]):
                if other_value is None:
                    continue
                if type(other_value) != first_type:
                    raise TypeError(
                        f"Literal annotation contains values of conflicting types: "
                        f"index 0 ({args[0]}) is {first_type}, index {idx + 1} ({other_value}) is {type(other_value)}"
                    )
            return rich_to_pyarrow(
                first_type,
                name,
                in_struct=in_struct,
                respect_nullability=respect_nullability,
            )

        if origin in (list, List, set, Set, frozenset, FrozenSet):
            typ_name = origin.__name__
            if len(args) == 0:
                raise TypeError(
                    f"{typ_name} types must be parameterised, e.g. {typ_name}[int]."
                )
            if len(args) > 1:
                raise TypeError(f"{typ_name} takes one type argument, got {len(args)}.")
            return pa.large_list(
                rich_to_pyarrow(
                    args[0],
                    name=f"{name}[]",
                    in_struct=in_struct,
                    respect_nullability=respect_nullability,
                )
            )

        if origin in (tuple, Tuple):
            if len(args) == 0:
                raise TypeError(
                    "Tuple types must be parameterised, e.g. Tuple[int, ...]."
                )
            if len(args) == 2 and args[1] is ...:
                return pa.large_list(
                    rich_to_pyarrow(
                        args[0],
                        name=f"{name}[]",
                        in_struct=in_struct,
                        respect_nullability=respect_nullability,
                    )
                )
            raise TypeError(
                "Only variable-length homogeneous tuples are supported (e.g. Tuple[int, ...])."
            )

        if origin in (dict, Dict, Mapping, collections.abc.Mapping):
            if len(args) != 2:
                raise TypeError(
                    "Dict types must have key and value annotations, e.g. Dict[str, int]."
                )
            key_type, value_type = args
            if _is_optional(key_type):
                raise TypeError(f"Dict keys cannot be optional, found: {key_type}")
            if not isinstance(key_type, type):
                raise TypeError(f"Dict keys must be a concrete type, found: {key_type}")
            pa_value_type = rich_to_pyarrow(
                value_type, "value", respect_nullability=respect_nullability
            )
            value_field = pa.field(
                "value", pa_value_type, nullable=_is_optional(value_type)
            )
            return pa.map_(
                key_type=rich_to_pyarrow(
                    key_type, "key", respect_nullability=respect_nullability
                ),
                item_type=value_field,
            )

        raise TypeError(f"Unsupported generic type: {origin}")

    # ---- Bare unparameterised generics (error) ----
    if python_type in (list, List, set, Set, frozenset, FrozenSet):
        typ_name = python_type.__name__
        raise TypeError(f"{typ_name} must be parameterised, e.g. {typ_name}[str].")
    if python_type in (tuple, Tuple, dict, Dict, Mapping, collections.abc.Mapping):
        typ_name = python_type.__name__
        raise TypeError(f"{typ_name} must be parameterised, e.g. {typ_name}[str, int].")

    if not isinstance(python_type, type):
        raise TypeError(f"Type annotations must be a type, got {python_type!r}.")

    # ---- Enums ----
    if issubclass(python_type, enum.Enum):
        return rich_to_pyarrow(
            _get_enum_value_type(python_type),
            name,
            in_struct=in_struct,
            respect_nullability=respect_nullability,
        )

    # ---- Structured types (dataclass, namedtuple, TypedDict, attrs, pydantic) ----
    if (
        dataclasses.is_dataclass(python_type)
        or _is_namedtuple(python_type)
        or is_typeddict(python_type)
        or _has_attrs(python_type)
        or _is_pydantic_basemodel(python_type)
    ):
        annotations = typing.get_type_hints(python_type)
        fields: list[pa.Field] = []
        for field_name, type_annotation in annotations.items():
            if field_name.startswith("__"):
                continue
            underlying = rich_to_pyarrow(
                type_annotation,
                name=f"{name}.{field_name}",
                in_struct=True,
                respect_nullability=respect_nullability,
            )
            fields.append(
                pa.field(
                    field_name,
                    underlying,
                    not respect_nullability or _NoneType in get_args(type_annotation),
                )
            )
        return pa.struct(fields)

    # ---- Protobuf messages ----
    if _is_protobuf_message(python_type):
        return _convert_proto_descriptor_to_pyarrow(python_type.DESCRIPTOR)

    # ---- Scalar / primitive types ----
    if issubclass(python_type, type(None)):
        return pa.null()
    if issubclass(python_type, str):
        return pa.large_utf8()
    if issubclass(python_type, bool):
        return pa.bool_()

    try:
        import numpy as np

        if issubclass(python_type, np.bool_):
            return pa.bool_()
    except ImportError:
        pass

    if issubclass(python_type, int):
        return pa.int64()
    if issubclass(python_type, float):
        return pa.float64()
    if issubclass(python_type, datetime):
        return pa.timestamp("us", "UTC")
    if issubclass(python_type, date):
        return pa.date32() if in_struct else pa.date64()
    if issubclass(python_type, time):
        return pa.time64("us")
    if issubclass(python_type, timedelta):
        return pa.duration("us")
    if issubclass(python_type, bytes):
        return pa.large_binary()
    if issubclass(python_type, decimal.Decimal):
        return pa.large_utf8()
    if issubclass(python_type, uuid.UUID):
        return pa.large_utf8()
    if issubclass(python_type, ipaddress.IPv4Address):
        return pa.uint32()
    if issubclass(python_type, ipaddress.IPv6Address):
        return pa.large_utf8()

    raise TypeError(
        f"Unable to determine the PyArrow type for field '{name}' with type `{python_type}`."
    )
