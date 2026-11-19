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
    """Strip Annotated metadata and optionality before choosing a scalar codec."""
    from chalkcompute._arrow_runtime import _chalk_unwrap

    return _chalk_unwrap(annotation)


def has_collection_annotation(annotation: Any) -> bool:
    """Whether the wire schema needs Python collection metadata."""
    from chalkcompute._arrow_runtime import _chalk_needs_codec

    return _chalk_needs_codec(annotation, collections_only=True)


def arrow_encoder_for_annotation(annotation: Any) -> Callable[[Any], Any] | None:
    from functools import cache
    from chalkcompute._arrow_runtime import _chalk_encoder

    if not has_collection_annotation(annotation):
        return _chalk_encoder(annotation)

    # Import PyArrow when the first value is serialized; registration only requires
    # the Arrow proto package.
    @cache
    def encoder() -> Callable[[Any], Any]:
        from chalkcompute._arrow_schema import rich_to_arrow_schema
        from chalkcompute._catalog_collections import _pyarrow_type_from_field

        field = rich_to_arrow_schema({"value": annotation}).columns[0]
        return _chalk_encoder(annotation, dtype=_pyarrow_type_from_field(field))

    return lambda value: encoder()(value)


def arrow_decoder_for_annotation(annotation: Any) -> Callable[[Any], Any] | None:
    from chalkcompute._arrow_runtime import _chalk_decoder

    return _chalk_decoder(annotation)


def handler_runtime_source() -> str:
    """Ship the same codecs to workers whose images omit the SDK."""
    from importlib.resources import files

    return (
        files("chalkcompute").joinpath("_arrow_runtime.py").read_text(encoding="utf-8")
        + "\n"
    )
