"""Runtime helpers for user-defined Arrow-backed values.

Classes opt in by declaring a concrete ``chalk.arrow.v1.ArrowType`` and are
expected to provide a pair of PyArrow scalar serialization dunders::

    class Counter:
        __chalk_arrow_type__ = arrow_pb.ArrowType(
            int64=arrow_pb.EmptyMessage()
        )

        def __chalk_serialize__(self) -> pa.Scalar:
            return pa.scalar(self.value, type=pa.int64())

        @classmethod
        def __chalk_deserialize__(cls, value: pa.Scalar) -> "Counter":
            return cls(value.as_py())

Classes used inside an annotated container can additionally expose a PyArrow
type factory so empty containers retain their element type::

    __chalk_pyarrow_type__ = staticmethod(pa.int64)

The proto is the catalog schema contract; PyArrow scalars are the Arrow IPC
value contract.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, get_args, get_origin


_ARROW_TYPE_ATTRIBUTE = "__chalk_arrow_type__"


def has_chalk_arrow_contract(annotation: Any) -> bool:
    """Return whether a class carries a typed Chalk Arrow declaration."""
    if not isinstance(annotation, type):
        return False
    from chalkcompute._gen.chalk.arrow.v1 import arrow_pb

    return isinstance(
        getattr(annotation, _ARROW_TYPE_ATTRIBUTE, None), arrow_pb.ArrowType
    )


def chalk_arrow_type(annotation: Any) -> Any:
    """Return the Arrow proto carried by the internal marker."""
    return annotation.__chalk_arrow_type__


def unwrap_result_annotation(annotation: Any) -> Any:
    """Return the item annotation for generator return annotations."""
    import collections.abc as cabc

    origin = get_origin(annotation)
    if origin in (
        cabc.Iterator,
        cabc.Generator,
        cabc.AsyncIterator,
        cabc.AsyncGenerator,
    ):
        args = get_args(annotation)
        if args:
            return args[0]
    return annotation


def unwrap_annotation(annotation: Any) -> Any:
    """Strip ``Annotated[...]`` metadata and ``| None`` from an annotation.

    An Arrow-backed class is still Arrow-backed when it is optional or carries
    ``Annotated`` metadata, and a nullable column decodes to either the class or
    ``None``. Mirrors ``_arrow_schema._unwrap_optional``, which decides the same
    question for the catalog schema, so the schema and the runtime serde cannot
    disagree about whether a column is Arrow-backed.
    """
    import types
    import typing

    if type(annotation) is type(typing.Annotated[int, ""]) and hasattr(
        annotation, "__metadata__"
    ):
        annotation = get_args(annotation)[0]

    origin = get_origin(annotation)
    if origin is not typing.Union and origin is not getattr(types, "UnionType", None):
        return annotation
    args = [arg for arg in get_args(annotation) if arg is not type(None)]
    return unwrap_annotation(args[0]) if len(args) == 1 else annotation


def _list_item_annotation(annotation: Any) -> Any | None:
    """Return the item annotation when ``annotation`` is a concrete list."""
    if get_origin(annotation) is not list:
        return None
    args = get_args(annotation)
    if len(args) != 1:
        return None
    return args[0]


def _pyarrow_type_for_annotation(annotation: Any) -> Any | None:
    annotation = unwrap_annotation(annotation)
    if has_chalk_arrow_contract(annotation):
        factory = getattr(annotation, "__chalk_pyarrow_type__", None)
        return None if factory is None else factory()

    item_annotation = _list_item_annotation(annotation)
    if item_annotation is None:
        return None
    item_type = _pyarrow_type_for_annotation(item_annotation)
    if item_type is None:
        return None

    import pyarrow as pa

    return pa.large_list(pa.field("item", item_type, nullable=False))


def arrow_encoder_for_annotation(
    annotation: Any,
) -> Callable[[Any], Any] | None:
    """Build a scalar encoder for an Arrow-backed annotation, recursively."""
    annotation = unwrap_annotation(annotation)
    if has_chalk_arrow_contract(annotation):
        return annotation.__chalk_serialize__

    item_annotation = _list_item_annotation(annotation)
    if item_annotation is None:
        return None
    item_encoder = arrow_encoder_for_annotation(item_annotation)
    if item_encoder is None:
        return None
    item_encoder_fn = item_encoder

    def encode_list(values: Any) -> Any:
        import pyarrow as pa

        if not isinstance(values, list):
            raise TypeError("a list annotation requires a list result")
        encoded = [item_encoder_fn(value) for value in values]
        list_type = _pyarrow_type_for_annotation(annotation)
        if list_type is None:
            if not encoded:
                raise TypeError(
                    "empty lists of Arrow-backed values require the element class "
                    "to define __chalk_pyarrow_type__"
                )
            list_type = pa.large_list(pa.field("item", encoded[0].type, nullable=False))
        return pa.scalar(encoded, type=list_type)

    return encode_list


def arrow_decoder_for_annotation(
    annotation: Any,
) -> Callable[[Any], Any] | None:
    """Build a scalar decoder for an Arrow-backed annotation, recursively."""
    annotation = unwrap_annotation(annotation)
    if has_chalk_arrow_contract(annotation):
        return annotation.__chalk_deserialize__

    item_annotation = _list_item_annotation(annotation)
    if item_annotation is None:
        return None
    item_decoder = arrow_decoder_for_annotation(item_annotation)
    if item_decoder is None:
        return None
    item_decoder_fn = item_decoder

    def decode_list(value: Any) -> list[Any]:
        return [
            None if not item.is_valid else item_decoder_fn(item)
            for item in value.values
        ]

    return decode_list


def handler_runtime_source() -> str:
    """Return self-contained serde helpers for a generated worker shim.

    Function images running in source-stripping mode do not necessarily have
    the Chalk SDK installed. Schema construction consumes the proto declaration;
    the embedded worker runtime trusts the scalar dunders.
    """
    import textwrap

    return textwrap.dedent(
        """\
        import collections.abc as _chalk_cabc
        import types as _chalk_types
        import typing as _chalk_typing


        def _chalk_has_contract(annotation):
            return (
                isinstance(annotation, type)
                and hasattr(annotation, "__chalk_arrow_type__")
            )


        def _chalk_unwrap(annotation):
            # An Arrow-backed class stays Arrow-backed under Annotated or | None.
            # Mirrors unwrap_annotation in chalkcompute._arrow_serde.
            if hasattr(annotation, "__metadata__"):
                args = _chalk_typing.get_args(annotation)
                if args:
                    annotation = args[0]
            origin = _chalk_typing.get_origin(annotation)
            union_type = getattr(_chalk_types, "UnionType", None)
            if origin is not _chalk_typing.Union and origin is not union_type:
                return annotation
            args = [
                arg
                for arg in _chalk_typing.get_args(annotation)
                if arg is not type(None)
            ]
            return _chalk_unwrap(args[0]) if len(args) == 1 else annotation


        def _chalk_to_pylist(column):
            return column.to_pylist()


        def _chalk_list_item_annotation(annotation):
            if _chalk_typing.get_origin(annotation) is not list:
                return None
            args = _chalk_typing.get_args(annotation)
            return args[0] if len(args) == 1 else None


        def _chalk_pyarrow_type(annotation):
            annotation = _chalk_unwrap(annotation)
            if _chalk_has_contract(annotation):
                factory = getattr(annotation, "__chalk_pyarrow_type__", None)
                return None if factory is None else factory()
            item_annotation = _chalk_list_item_annotation(annotation)
            if item_annotation is None:
                return None
            item_type = _chalk_pyarrow_type(item_annotation)
            if item_type is None:
                return None
            import pyarrow as pa
            return pa.large_list(pa.field("item", item_type, nullable=False))


        def _chalk_decoder(annotation):
            annotation = _chalk_unwrap(annotation)
            if _chalk_has_contract(annotation):
                return annotation.__chalk_deserialize__
            item_annotation = _chalk_list_item_annotation(annotation)
            if item_annotation is None:
                return None
            item_decoder = _chalk_decoder(item_annotation)
            if item_decoder is None:
                return None
            return lambda value: [
                None if not item.is_valid else item_decoder(item)
                for item in value.values
            ]


        def _chalk_column_decoder(annotation):
            decoder = _chalk_decoder(annotation)
            if decoder is None:
                return _chalk_to_pylist
            return lambda column: [
                None if not value.is_valid else decoder(value)
                for value in column
            ]


        def _chalk_encoder(annotation):
            annotation = _chalk_unwrap(annotation)
            if _chalk_has_contract(annotation):
                return annotation.__chalk_serialize__
            item_annotation = _chalk_list_item_annotation(annotation)
            if item_annotation is None:
                return None
            item_encoder = _chalk_encoder(item_annotation)
            if item_encoder is None:
                return None

            def encode_list(values):
                import pyarrow as pa
                if not isinstance(values, list):
                    raise TypeError("a list annotation requires a list result")
                encoded = [item_encoder(value) for value in values]
                list_type = _chalk_pyarrow_type(annotation)
                if list_type is None:
                    if not encoded:
                        raise TypeError(
                            "empty lists of Arrow-backed values require the element "
                            "class to define __chalk_pyarrow_type__"
                        )
                    list_type = pa.large_list(
                        pa.field("item", encoded[0].type, nullable=False)
                    )
                return pa.scalar(encoded, type=list_type)

            return encode_list


        def _chalk_result_annotation(target):
            annotation = _chalk_typing.get_type_hints(
                target, include_extras=True
            ).get("return")
            origin = _chalk_typing.get_origin(annotation)
            if origin in (
                _chalk_cabc.Iterator,
                _chalk_cabc.Generator,
                _chalk_cabc.AsyncIterator,
                _chalk_cabc.AsyncGenerator,
            ):
                args = _chalk_typing.get_args(annotation)
                if args:
                    return args[0]
            return annotation
        """
    )
