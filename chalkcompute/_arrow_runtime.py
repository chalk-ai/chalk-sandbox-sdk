"""Self-contained Arrow scalar codecs shared by the SDK and generated workers."""

import collections.abc as _chalk_cabc
import dataclasses as _chalk_dataclasses
import decimal as _chalk_decimal
import enum as _chalk_enum
import ipaddress as _chalk_ip
import uuid as _chalk_uuid
import types as _chalk_types
import typing as _chalk_typing


def _chalk_unwrap(annotation):
    if _chalk_typing.get_origin(annotation) is _chalk_typing.Annotated:
        return _chalk_unwrap(_chalk_typing.get_args(annotation)[0])
    if _chalk_typing.get_origin(annotation) in (
        _chalk_typing.Union,
        _chalk_types.UnionType,
    ):
        args = [
            arg for arg in _chalk_typing.get_args(annotation) if arg is not type(None)
        ]
        if len(args) == 1:
            return _chalk_unwrap(args[0])
    return annotation


def _chalk_has_contract(annotation):
    return isinstance(annotation, type) and hasattr(annotation, "__chalk_arrow_type__")


def _chalk_struct_fields(annotation):
    if not isinstance(annotation, type):
        return {}
    if (
        _chalk_dataclasses.is_dataclass(annotation)
        or _chalk_typing.is_typeddict(annotation)
        or hasattr(annotation, "_fields")
        or hasattr(annotation, "__attrs_attrs__")
        or hasattr(annotation, "model_fields")
        or hasattr(annotation, "__fields__")
    ):
        return {
            name: value
            for name, value in _chalk_typing.get_type_hints(
                annotation, include_extras=True
            ).items()
            if not name.startswith("__")
        }
    return {}


def _chalk_needs_codec(annotation, *, collections_only=False, seen=()):
    annotation = _chalk_unwrap(annotation)
    origin = _chalk_typing.get_origin(annotation)
    if origin in (tuple, set, frozenset):
        return True
    if _chalk_has_contract(annotation):
        return not collections_only
    if annotation in seen:
        return False
    children = (
        _chalk_typing.get_args(annotation)
        if origin in (list, dict, _chalk_cabc.Mapping)
        else _chalk_struct_fields(annotation).values()
    )
    return any(
        _chalk_needs_codec(
            item, collections_only=collections_only, seen=(*seen, annotation)
        )
        for item in children
    )


def _chalk_result_annotation(target):
    annotation = _chalk_typing.get_type_hints(target, include_extras=True).get("return")
    if _chalk_typing.get_origin(annotation) in (
        _chalk_cabc.Iterator,
        _chalk_cabc.Generator,
        _chalk_cabc.AsyncIterator,
        _chalk_cabc.AsyncGenerator,
    ):
        args = _chalk_typing.get_args(annotation)
        if args:
            return args[0]
    return annotation


def _chalk_pyarrow_type(annotation):
    # Custom values can supply their physical dtype in a standalone worker.
    import pyarrow as pa

    annotation = _chalk_unwrap(annotation)
    if _chalk_has_contract(annotation):
        factory = getattr(annotation, "__chalk_pyarrow_type__", None)
        return None if factory is None else factory()
    if _chalk_typing.get_origin(annotation) is list:
        (item,) = _chalk_typing.get_args(annotation)
        item_type = _chalk_pyarrow_type(item)
        if item_type is not None:
            return pa.large_list(
                pa.field(
                    "item",
                    item_type,
                    nullable=type(None) in _chalk_typing.get_args(item),
                )
            )
    return None


def _chalk_converter(annotation, *, encoding):
    """Convert one non-null value between its annotated and Arrow representations."""
    annotation = _chalk_unwrap(annotation)
    if _chalk_has_contract(annotation):
        return (
            annotation.__chalk_serialize__
            if encoding
            else annotation.__chalk_deserialize__
        )
    origin = _chalk_typing.get_origin(annotation)
    args = _chalk_typing.get_args(annotation)
    if origin is tuple and not (len(args) == 2 and args[1] is ...):
        items = [_chalk_nullable_converter(item, encoding=encoding) for item in args]

        def fixed_tuple(value):
            if encoding:
                if not isinstance(value, tuple) or len(value) != len(items):
                    raise TypeError(f"a fixed tuple of length {len(items)} is required")
                return {
                    str(i): convert(item)
                    for i, (convert, item) in enumerate(zip(items, value))
                }
            return tuple(convert(value[str(i)]) for i, convert in enumerate(items))

        return fixed_tuple
    if origin in (list, set, frozenset, tuple):
        convert = _chalk_nullable_converter(args[0], encoding=encoding)

        def sequence(value):
            if encoding:
                if not isinstance(value, origin):
                    raise TypeError(
                        f"a {origin.__name__} annotation requires a {origin.__name__} result"
                    )
                return [convert(item) for item in value]
            return origin(convert(item) for item in value.values)

        return sequence
    if origin in (dict, _chalk_cabc.Mapping):
        key = _chalk_nullable_converter(args[0], encoding=encoding)
        item = _chalk_nullable_converter(args[1], encoding=encoding)
        if encoding:
            return lambda value: [(key(k), item(v)) for k, v in value.items()]
        return lambda value: {key(pair[0]): item(pair[1]) for pair in value.values}
    fields = {
        name: _chalk_nullable_converter(child, encoding=encoding)
        for name, child in _chalk_struct_fields(annotation).items()
    }
    if fields:
        if encoding:
            return lambda value: {
                name: convert(
                    value[name] if isinstance(value, dict) else getattr(value, name)
                )
                for name, convert in fields.items()
            }
        return lambda value: annotation(
            **{name: convert(value[name]) for name, convert in fields.items()}
        )
    if isinstance(annotation, type) and issubclass(annotation, _chalk_enum.Enum):
        item_type = type(next(iter(annotation)).value)
        convert = _chalk_converter(item_type, encoding=encoding)
        if encoding:
            return lambda value: convert(value.value)
        return lambda scalar: annotation(convert(scalar))
    if annotation in (_chalk_decimal.Decimal, _chalk_uuid.UUID, _chalk_ip.IPv6Address):
        return str if encoding else lambda scalar: annotation(scalar.as_py())
    if annotation is _chalk_ip.IPv4Address:
        return int if encoding else lambda scalar: annotation(scalar.as_py())
    return (lambda value: value) if encoding else (lambda scalar: scalar.as_py())


def _chalk_nullable_converter(annotation, *, encoding):
    convert = _chalk_converter(annotation, encoding=encoding)
    if encoding:
        return lambda value: None if value is None else convert(value)
    return lambda scalar: convert(scalar) if scalar.is_valid else None


def _chalk_decoder(annotation):
    if _chalk_needs_codec(annotation):
        return _chalk_nullable_converter(annotation, encoding=False)
    return None


def _chalk_to_pylist(column):
    return column.to_pylist()


def _chalk_column_decoder(annotation):
    decoder = _chalk_decoder(annotation)
    if decoder is None:
        return _chalk_to_pylist
    return lambda column: [decoder(value) for value in column]


def _chalk_encoder(annotation, dtype=None):
    if not _chalk_needs_codec(annotation):
        return None
    convert = _chalk_nullable_converter(annotation, encoding=True)

    def encode(value):
        import pyarrow as pa

        converted = convert(value)
        if isinstance(converted, pa.Scalar):
            return converted
        target = dtype if dtype is not None else _chalk_pyarrow_type(annotation)
        if (
            target is None
            and _chalk_typing.get_origin(_chalk_unwrap(annotation)) is list
            and converted
        ):
            target = pa.large_list(
                pa.field("item", pa.scalar(converted[0]).type, nullable=False)
            )
        if target is None and converted == []:
            raise TypeError(
                "empty lists of Arrow-backed values require the element class to define __chalk_pyarrow_type__"
            )
        return pa.scalar(converted, type=target)

    return encode
