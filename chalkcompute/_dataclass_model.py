"""JSON schemas and replies for dataclass scorers, without a Pydantic dependency."""

from __future__ import annotations

import dataclasses
from enum import Enum
from types import UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin, get_type_hints


def model_fields(cls: Any) -> dict[str, Any]:
    hints = get_type_hints(cls, include_extras=True)
    return {
        field.name: hints[field.name] for field in dataclasses.fields(cls) if field.init
    }


def json_schema(cls: Any, *, strict: bool = False) -> dict[str, Any]:
    definitions: dict[str, Any] = {}
    names: dict[type, str] = {}

    def schema(typ: Any) -> dict[str, Any]:
        origin, args = get_origin(typ), get_args(typ)
        if origin is Annotated:
            return schema(args[0])
        if origin in (Union, UnionType):
            return {"anyOf": [schema(arg) for arg in args]}
        if origin is Literal:
            values = [dump(arg) for arg in args]
            kinds = {type(value) for value in values}
            return {
                **(schema(next(iter(kinds))) if len(kinds) == 1 else {}),
                "enum": values,
            }
        if isinstance(typ, type) and issubclass(typ, Enum):
            values = [member.value for member in typ]
            kinds = {type(value) for value in values}
            return {
                **(schema(next(iter(kinds))) if len(kinds) == 1 else {}),
                "enum": values,
                "title": typ.__name__,
            }
        if typ is Any:
            return {}
        primitives = {
            str: "string",
            int: "integer",
            float: "number",
            bool: "boolean",
            type(None): "null",
        }
        if typ in primitives:
            return {"type": primitives[typ]}
        if origin is list:
            return {"type": "array", "items": schema(args[0])}
        if origin is dict and args[0] is str:
            return {"type": "object", "additionalProperties": schema(args[1])}
        if isinstance(typ, type) and dataclasses.is_dataclass(typ):
            if typ not in names:
                name = typ.__name__
                while name in definitions:
                    name += "_"
                names[typ] = name
                # Reserve before descending into recursive fields.
                definitions[name] = {}
                properties = {}
                hints = model_fields(typ)
                required = []
                for field in dataclasses.fields(typ):
                    if not field.init:
                        continue
                    properties[field.name] = schema(hints[field.name])
                    if "description" in field.metadata:
                        properties[field.name] = {
                            **properties[field.name],
                            "description": field.metadata["description"],
                        }
                    if strict or (
                        field.default is dataclasses.MISSING
                        and field.default_factory is dataclasses.MISSING
                    ):
                        required.append(field.name)
                definitions[name] = {
                    "type": "object",
                    "title": typ.__name__,
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                }
            return {"$ref": f"#/$defs/{names[typ]}"}
        raise TypeError(f"Unsupported dataclass response field type: {typ!r}")

    schema(cls)
    # Keep the root in $defs too: a nested field can refer back to it.
    return {**definitions[names[cls]], "$defs": definitions}


def validate(typ: Any, data: Any, *, path: str = "response") -> Any:
    """Construct nested dataclasses and reject values that contradict annotations."""
    origin, args = get_origin(typ), get_args(typ)
    if origin is Annotated:
        return validate(args[0], data, path=path)
    if origin in (Union, UnionType):
        for arg in args:
            try:
                return validate(arg, data, path=path)
            except (TypeError, ValueError):
                pass
    elif origin is Literal:
        for value in args:
            serialized = dump(value)
            if type(data) is type(serialized) and data == serialized:
                return value
            if isinstance(value, Enum) and data is value:
                return value
    elif isinstance(typ, type) and issubclass(typ, Enum):
        return typ(data)
    elif typ is Any:
        return data
    elif typ in (str, int, float, bool, type(None)):
        if type(data) is typ or (typ is float and type(data) is int):
            return data
    elif origin is list and isinstance(data, list):
        return [
            validate(args[0], item, path=f"{path}[{i}]") for i, item in enumerate(data)
        ]
    elif origin is dict and isinstance(data, dict):
        return {
            validate(args[0], key, path=f"{path}.key"): validate(
                args[1], value, path=f"{path}[{key!r}]"
            )
            for key, value in data.items()
        }
    elif isinstance(typ, type) and dataclasses.is_dataclass(typ):
        if isinstance(data, typ):
            for name, hint in model_fields(typ).items():
                validate(hint, getattr(data, name), path=f"{path}.{name}")
            return data
        if isinstance(data, dict):
            fields = model_fields(typ)
            extra = data.keys() - fields.keys()
            if extra:
                raise ValueError(f"{path}: unexpected fields {sorted(extra)}")
            values = {
                name: validate(fields[name], value, path=f"{path}.{name}")
                for name, value in data.items()
            }
            return typ(**values)
    raise ValueError(f"{path}: expected {typ!r}, got {data!r}")


def dump(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {name: dump(getattr(value, name)) for name in model_fields(type(value))}
    if isinstance(value, Enum):
        return dump(value.value)
    if isinstance(value, list):
        return [dump(item) for item in value]
    if isinstance(value, dict):
        return {key: dump(item) for key, item in value.items()}
    return value
