"""Arrow field codecs for Python collections on attached catalog functions."""

import inspect
import json
from types import GenericAlias
from typing import Any


def _pyarrow_field(field):
    """Build a PyArrow field from its Arrow proto JSON, also in stripped workers."""
    import pyarrow as pa

    ((kind, data),) = field["arrowType"].items()
    if kind == "struct":
        dtype = pa.struct([_pyarrow_field(f) for f in data.get("subFieldTypes", [])])
    elif kind in ("list", "largeList", "fixedSizeList"):
        child = _pyarrow_field(data["fieldType"])
        dtype = (
            pa.large_list(child)
            if kind == "largeList"
            else pa.list_(
                child, data.get("listSize", 0) if kind == "fixedSizeList" else -1
            )
        )
    elif kind == "map":
        dtype = pa.map_(
            _pyarrow_field(data["keyField"]),
            _pyarrow_field(data["itemField"]),
            keys_sorted=data.get("keysSorted", False),
        )
    elif kind in ("decimal128", "decimal256"):
        factory = pa.decimal128 if kind == "decimal128" else pa.decimal256
        dtype = factory(data["precision"], data.get("scale", 0))
    elif kind == "fixedSizeBinary":
        dtype = pa.binary(data)
    elif kind in ("timestamp", "time32", "time64", "duration"):
        units = {
            "TIME_UNIT_SECOND": "s",
            "TIME_UNIT_MILLISECOND": "ms",
            "TIME_UNIT_MICROSECOND": "us",
            "TIME_UNIT_NANOSECOND": "ns",
        }
        if kind == "timestamp":
            dtype = pa.timestamp(
                units[data["timeUnit"]], tz=data.get("timezone") or None
            )
        else:
            dtype = getattr(pa, kind)(units[data])
    else:
        aliases = {
            "none": "null",
            "largeUtf8": "large_string",
            "largeBinary": "large_binary",
        }
        dtype = pa.type_for_alias(aliases.get(kind, kind))
    return pa.field(
        field.get("name", ""),
        dtype,
        nullable=field.get("nullable", False),
        metadata=field.get("metadata"),
    )


def _pyarrow_type_from_field(field):
    return _pyarrow_field(json.loads(field.to_json())).type


def _has_wire_marker(field: dict) -> bool:
    metadata = field.get("metadata", {})
    if "chalk.python.collection" in metadata or "chalk.python.record" in metadata:
        return True
    ((kind, data),) = (field.get("arrowType") or {"none": {}}).items()
    if kind in ("list", "largeList", "fixedSizeList"):
        return _has_wire_marker(data["fieldType"])
    if kind == "struct":
        return any(_has_wire_marker(child) for child in data.get("subFieldTypes", []))
    if kind == "map":
        return _has_wire_marker(data["keyField"]) or _has_wire_marker(data["itemField"])
    return False


def collection_output_field(func):
    from typing import get_type_hints

    from ._arrow_schema import rich_to_arrow_schema
    from ._arrow_serde import has_typed_wire_annotation, unwrap_result_annotation

    annotation = unwrap_result_annotation(
        get_type_hints(func, include_extras=True).get("return")
    )
    if has_typed_wire_annotation(annotation):
        return rich_to_arrow_schema({"result": annotation}).columns[0]
    return None


def output_dtype_source(field: Any | None) -> str:
    if field is not None:
        spec = json.loads(field.to_json())
        if _has_wire_marker(spec):
            return (
                inspect.getsource(_pyarrow_field)
                + f"""
_output_dtype = _pyarrow_field({spec!r}).type


def _output_rows(rows):
    import pyarrow as pa
    return pa.array([row.as_py() for row in rows], type=_output_dtype)


def _output_item(scalar):
    import pyarrow as pa
    return pa.repeat(scalar, 1)
"""
            )
    return """_output_dtype = None
def _output_rows(rows):
    return rows

def _output_item(item):
    return item
"""


def _collection_annotation(field: dict):
    """Reconstruct the collection constructors a catalog-only handle can observe."""
    from typing import TypedDict

    ((kind, data),) = field["arrowType"].items()
    marker = field.get("metadata", {}).get("chalk.python.collection")
    if kind in ("list", "largeList", "fixedSizeList"):
        item = _collection_annotation(data["fieldType"])
        origins = {None: list, "set": set, "frozenset": frozenset}
        if marker == "tuple":
            return tuple[item, ...]
        if marker not in origins:
            raise ValueError(f"Unsupported catalog collection {marker!r}")
        return origins[marker][item]
    if kind == "struct":
        fields = data.get("subFieldTypes", [])
        if marker == "tuple":
            if any(f.get("name") != str(i) for i, f in enumerate(fields)):
                raise ValueError("Catalog tuple fields must be named by position")
            return GenericAlias(tuple, tuple(_collection_annotation(f) for f in fields))
        if marker is None:
            children = {f["name"]: _collection_annotation(f) for f in fields}
            return TypedDict("CatalogStruct", children)  # ty: ignore[invalid-argument-type]
    if marker is not None:
        raise ValueError(f"Collection marker {marker!r} is incompatible with {kind!r}")
    if kind == "map":
        return GenericAlias(
            dict,
            (
                _collection_annotation(data["keyField"]),
                _collection_annotation(data["itemField"]),
            ),
        )
    return object


def catalog_collection_codec(field, *, encoding: bool):
    from ._arrow_runtime import _chalk_decoder, _chalk_encoder

    spec = json.loads(field.to_json())
    if not _has_wire_marker(spec):
        return None
    annotation = _collection_annotation(spec)
    if not encoding:
        return _chalk_decoder(annotation, from_schema=True)

    # Attaching to a function only needs its catalog metadata. Import PyArrow
    # when a caller actually sends Arrow values.
    from functools import lru_cache

    @lru_cache(maxsize=1)
    def encoder():
        return _chalk_encoder(
            annotation, dtype=_pyarrow_field(spec).type, from_schema=True
        )

    return lambda value: encoder()(value)
