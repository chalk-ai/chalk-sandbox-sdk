"""Runtime helpers for user-defined Arrow-backed values.

Classes opt in by declaring a concrete ``pyarrow.DataType`` and a pair of
serialization dunders::

    class Point:
        __chalk_arrow_type__ = pa.struct(
            [
                pa.field("x", pa.int64(), nullable=False),
                pa.field("y", pa.int64(), nullable=False),
            ]
        )

        def __chalk_serialize__(self) -> pa.Scalar:
            return pa.scalar(
                {"x": self.x, "y": self.y},
                type=self.__chalk_arrow_type__,
            )

        @classmethod
        def __chalk_deserialize__(cls, value: pa.Scalar) -> "Point":
            data = value.as_py()
            return cls(data["x"], data["y"])

The Arrow type, rather than Python annotations on the dunder methods, is the
wire contract. This module intentionally has no imports from the rest of the
SDK so the same rules can be embedded in generated function handlers.
"""

from __future__ import annotations

import inspect
import typing
from typing import Any, get_args, get_origin


_ARROW_TYPE_ATTRIBUTE = "__chalk_arrow_type__"
_SERIALIZE_METHOD = "__chalk_serialize__"
_DESERIALIZE_METHOD = "__chalk_deserialize__"


def has_chalk_arrow_contract(annotation: Any) -> bool:
    """Return whether a class declares the complete Chalk Arrow contract."""
    if not isinstance(annotation, type):
        return False
    return all(
        hasattr(annotation, name)
        for name in (
            _ARROW_TYPE_ATTRIBUTE,
            _SERIALIZE_METHOD,
            _DESERIALIZE_METHOD,
        )
    )


def chalk_arrow_type(annotation: Any) -> Any:
    """Validate *annotation* and return its declared ``pyarrow.DataType``.

    Raises
    ------
    TypeError
        If the class declares an incomplete or malformed contract.
    """
    import pyarrow as pa

    if not isinstance(annotation, type):
        raise TypeError(
            "Chalk Arrow serialization can only be declared on a class, "
            f"got {annotation!r}."
        )

    type_name = f"{annotation.__module__}.{annotation.__qualname__}"
    arrow_type = inspect.getattr_static(annotation, _ARROW_TYPE_ATTRIBUTE, None)
    if not isinstance(arrow_type, pa.DataType):
        raise TypeError(
            f"{type_name}.{_ARROW_TYPE_ATTRIBUTE} must be a pyarrow.DataType, "
            f"got {type(arrow_type).__name__}."
        )

    serialize = inspect.getattr_static(annotation, _SERIALIZE_METHOD, None)
    if serialize is None or isinstance(serialize, (classmethod, staticmethod)):
        raise TypeError(f"{type_name}.{_SERIALIZE_METHOD} must be an instance method.")
    if not callable(getattr(annotation, _SERIALIZE_METHOD, None)):
        raise TypeError(f"{type_name}.{_SERIALIZE_METHOD} must be callable.")
    if inspect.iscoroutinefunction(getattr(annotation, _SERIALIZE_METHOD)):
        raise TypeError(f"{type_name}.{_SERIALIZE_METHOD} must be synchronous.")

    deserialize = inspect.getattr_static(annotation, _DESERIALIZE_METHOD, None)
    if not isinstance(deserialize, classmethod):
        raise TypeError(f"{type_name}.{_DESERIALIZE_METHOD} must be a classmethod.")
    if inspect.iscoroutinefunction(getattr(annotation, _DESERIALIZE_METHOD)):
        raise TypeError(f"{type_name}.{_DESERIALIZE_METHOD} must be synchronous.")

    return arrow_type


def serialize_value(value: Any, annotation: Any) -> Any:
    """Serialize one typed function value to its Arrow scalar representation."""
    import pyarrow as pa

    if not has_chalk_arrow_contract(annotation):
        return value

    expected_type = chalk_arrow_type(annotation)
    if not isinstance(value, annotation):
        raise TypeError(
            f"Expected a {annotation.__module__}.{annotation.__qualname__}, "
            f"got {type(value).__module__}.{type(value).__qualname__}."
        )

    serialized = value.__chalk_serialize__()
    if not isinstance(serialized, pa.Scalar):
        raise TypeError(
            f"{annotation.__module__}.{annotation.__qualname__}.{_SERIALIZE_METHOD} "
            f"must return a pyarrow.Scalar, got {type(serialized).__name__}."
        )
    if not serialized.type.equals(expected_type):
        raise TypeError(
            f"{annotation.__module__}.{annotation.__qualname__}.{_SERIALIZE_METHOD} "
            f"returned Arrow type {serialized.type}, expected {expected_type}."
        )
    return serialized


def deserialize_scalar(value: Any, annotation: Any) -> Any:
    """Deserialize one Arrow scalar according to a function annotation."""
    import pyarrow as pa

    if not isinstance(value, pa.Scalar):
        raise TypeError(
            "Chalk Arrow deserialization requires a pyarrow.Scalar, "
            f"got {type(value).__name__}."
        )
    if not value.is_valid:
        return None

    candidate = annotation
    if candidate is None or not has_chalk_arrow_contract(candidate):
        return value.as_py()

    expected_type = chalk_arrow_type(candidate)
    if not value.type.equals(expected_type):
        raise TypeError(
            f"Cannot deserialize Arrow type {value.type} as "
            f"{candidate.__module__}.{candidate.__qualname__}; expected "
            f"{expected_type}."
        )
    return candidate.__chalk_deserialize__(value)


def function_result_annotation(func: Any) -> Any | None:
    """Return a function's scalar return or generator yield annotation."""
    import collections.abc as cabc

    hints = typing.get_type_hints(func, include_extras=True)
    annotation = hints.get("return")
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
    the Chalk SDK installed. Embedding this small duck-typed runtime keeps the
    public dunder contract usable there without coupling it to the worker
    package release cycle.
    """
    import textwrap

    return textwrap.dedent(
        """\
        import collections.abc as _chalk_cabc
        import typing as _chalk_typing
        import pyarrow as _chalk_pa


        def _chalk_contract_type(annotation):
            if not isinstance(annotation, type):
                return None
            names = (
                "__chalk_arrow_type__",
                "__chalk_serialize__",
                "__chalk_deserialize__",
            )
            if not all(hasattr(annotation, name) for name in names):
                return None
            arrow_type = getattr(annotation, "__chalk_arrow_type__", None)
            if not isinstance(arrow_type, _chalk_pa.DataType):
                raise TypeError(
                    f"{annotation.__module__}.{annotation.__qualname__}."
                    "__chalk_arrow_type__ must be a pyarrow.DataType"
                )
            if not callable(getattr(annotation, "__chalk_serialize__", None)):
                raise TypeError(
                    f"{annotation.__module__}.{annotation.__qualname__}."
                    "__chalk_serialize__ must be an instance method"
                )
            if not callable(getattr(annotation, "__chalk_deserialize__", None)):
                raise TypeError(
                    f"{annotation.__module__}.{annotation.__qualname__}."
                    "__chalk_deserialize__ must be a classmethod"
                )
            return annotation


        def _chalk_deserialize_scalar(value, annotation):
            if not value.is_valid:
                return None
            contract_type = _chalk_contract_type(annotation)
            if contract_type is None:
                return value.as_py()
            expected = contract_type.__chalk_arrow_type__
            if not value.type.equals(expected):
                raise TypeError(
                    f"Cannot deserialize Arrow type {value.type} as "
                    f"{contract_type.__module__}.{contract_type.__qualname__}; "
                    f"expected {expected}"
                )
            return contract_type.__chalk_deserialize__(value)


        def _chalk_serialize_value(value, annotation):
            contract_type = _chalk_contract_type(annotation)
            if contract_type is None:
                return value
            if not isinstance(value, contract_type):
                raise TypeError(
                    f"Expected {contract_type.__module__}."
                    f"{contract_type.__qualname__}, got {type(value).__module__}."
                    f"{type(value).__qualname__}"
                )
            serialized = value.__chalk_serialize__()
            if not isinstance(serialized, _chalk_pa.Scalar):
                raise TypeError(
                    f"{contract_type.__module__}.{contract_type.__qualname__}."
                    "__chalk_serialize__ must return a pyarrow.Scalar"
                )
            expected = contract_type.__chalk_arrow_type__
            if not serialized.type.equals(expected):
                raise TypeError(
                    f"{contract_type.__module__}.{contract_type.__qualname__}."
                    f"__chalk_serialize__ returned Arrow type {serialized.type}, "
                    f"expected {expected}"
                )
            return serialized


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
