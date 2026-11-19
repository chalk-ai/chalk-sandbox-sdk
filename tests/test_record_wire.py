"""Catalog record registration and end-to-end worker IPC."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from types import ModuleType
from typing import ClassVar

import pytest
from chalk_remote_call.arrow_utils import decode_ipc_stream
from chalk_remote_call.servicer import process_batches
from pydantic import BaseModel, Field

from chalkcompute._arrow_schema import rich_to_arrow_schema
from chalkcompute._function import (
    RemoteFunction,
    _apply_function_version,
    _build_schemas,
    _generate_handler_shim,
)
from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import service_pb


@dataclass
class Address:
    country: ClassVar[str] = "US"
    street: str
    zip: int | None


class Person(BaseModel):
    name: str
    age: int | None


def addresses(items: list[Address | None]) -> list[Address | None]:
    return [None if a is None else Address(a.street.upper(), a.zip) for a in items]


def person(p: Person) -> Person:
    return Person(name=p.name.upper(), age=p.age)


@pytest.mark.parametrize("attached", [False, True])
@pytest.mark.parametrize(
    ("target", "kind", "cls", "value", "expected", "wire_in", "wire_out"),
    [
        (
            addresses,
            "dataclass",
            Address,
            [Address("main", None), None],
            [Address("MAIN", None), None],
            [{"street": "main", "zip": None}, None],
            [{"street": "MAIN", "zip": None}, None],
        ),
        (
            person,
            "pydantic",
            Person,
            Person(name="ada", age=27),
            Person(name="ADA", age=27),
            {"name": "ada", "age": 27},
            {"name": "ADA", "age": 27},
        ),
    ],
)
def test_record_calls_roundtrip(
    monkeypatch, attached, target, kind, cls, value, expected, wire_in, wire_out
):
    inputs, outputs = _build_schemas(target)
    for field in (inputs.columns[0], outputs.columns[0]):
        if target is addresses:
            field = field.arrow_type.arrow_type_enum.value.field_type
        assert field.metadata == {
            "chalk.python.record": kind,
            "chalk.python.type": f"{cls.__module__}.{cls.__qualname__}",
        }

    handle = RemoteFunction(target)
    if attached:
        handle = RemoteFunction._make_empty()
        _apply_function_version(
            handle,
            service_pb.ExternalFunctionVersion(
                function_name=target.__name__,
                input_arrow_schema=inputs,
                output_arrow_schema=outputs,
            ),
            is_current=True,
        )
    module = ModuleType("user_module")
    setattr(module, target.__name__, target)
    monkeypatch.setitem(sys.modules, "user_module", module)
    namespace = {}
    exec(
        _generate_handler_shim(target.__name__, output_field=outputs.columns[0]),
        namespace,
    )
    chunks = process_batches(
        handle._serialize_call_args(wire_in if attached else value),
        namespace["handler"],
        None,
        {},
    )
    (batch,) = decode_ipc_stream(chunks[0])
    assert batch.schema.names == ["result"]
    assert batch.column(0).to_pylist() == [wire_out]
    assert handle._decode_result_chunks(chunks) == [wire_out if attached else expected]


def test_model_alias_keeps_legacy_wire_contract():
    class Aliased(BaseModel):
        age: int = Field(alias="years")

    field = rich_to_arrow_schema({"p": Aliased}).columns[0]
    assert "chalk.python.record" not in field.metadata


def test_post_init_and_validators_keep_legacy_wire_contract():
    from pydantic import field_validator

    @dataclass
    class Bump:
        n: int

        def __post_init__(self):
            self.n += 1

    class Model(BaseModel):
        n: int

        @field_validator("n")
        @classmethod
        def bump(cls, value):
            return value + 1

    schema = rich_to_arrow_schema({"dc": Bump, "model": Model})
    assert all("chalk.python.record" not in field.metadata for field in schema.columns)


def test_model_with_extra_fields_keeps_legacy_wire_contract():
    from pydantic import ConfigDict

    class OpenRecord(BaseModel):
        model_config = ConfigDict(extra="allow")
        name: str

    field = rich_to_arrow_schema({"p": OpenRecord}).columns[0]
    assert "chalk.python.record" not in field.metadata
