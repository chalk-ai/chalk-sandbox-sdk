"""Runtime helpers for user-defined Arrow-backed values.

Classes opt in by declaring a concrete ``chalk.arrow.v1.ArrowType`` and are
expected to provide a pair of PyArrow scalar serialization dunders::

    class Counter:
        __chalk_arrow_type__ = arrow_pb2.ArrowType(
            int64=arrow_pb2.EmptyMessage()
        )

        def __chalk_serialize__(self) -> pa.Scalar:
            return pa.scalar(self.value, type=pa.int64())

        @classmethod
        def __chalk_deserialize__(cls, value: pa.Scalar) -> "Counter":
            return cls(value.as_py())

The proto is the catalog schema contract; PyArrow scalars are the Arrow IPC
value contract.
"""

from __future__ import annotations

from typing import Any, get_args, get_origin


_ARROW_TYPE_ATTRIBUTE = "__chalk_arrow_type__"


def has_chalk_arrow_contract(annotation: Any) -> bool:
    """Return whether a class carries Chalk's internal Arrow marker."""
    return isinstance(annotation, type) and hasattr(annotation, _ARROW_TYPE_ATTRIBUTE)


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
        import typing as _chalk_typing


        def _chalk_has_contract(annotation):
            return (
                isinstance(annotation, type)
                and hasattr(annotation, "__chalk_arrow_type__")
            )


        def _chalk_to_pylist(column):
            return column.to_pylist()


        def _chalk_column_decoder(annotation):
            if not _chalk_has_contract(annotation):
                return _chalk_to_pylist
            deserialize = annotation.__chalk_deserialize__
            return lambda column: [
                None if not value.is_valid else deserialize(value)
                for value in column
            ]


        def _chalk_encoder(annotation):
            if not _chalk_has_contract(annotation):
                return None
            return annotation.__chalk_serialize__


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
