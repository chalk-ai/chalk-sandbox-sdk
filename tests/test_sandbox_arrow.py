from unittest.mock import Mock, patch

import pyarrow as pa
import pytest

from chalkcompute import Sandbox
from chalkcompute._arrow_serde import (
    arrow_decoder_for_annotation,
    arrow_encoder_for_annotation,
    handler_runtime_source,
)
from chalkcompute._sandbox import SandboxError


def test_sandbox_ipc_roundtrip_omits_credentials_and_binds_lazily():
    creator = Mock()
    sandbox = Sandbox(client=creator, sandbox_id="sandbox-123")
    sandbox._env = {"SECRET": "never-serialize-this"}
    scalar = arrow_encoder_for_annotation(Sandbox)(sandbox)
    table = pa.table({"result": pa.array([scalar], type=scalar.type)})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    ipc = sink.getvalue()
    assert b"never-serialize-this" not in ipc.to_pybytes()
    with patch("chalkcompute._sandbox.SandboxClient.from_env") as bind:
        restored = arrow_decoder_for_annotation(Sandbox)(
            pa.ipc.open_stream(ipc).read_all().column(0)[0]
        )
        assert restored.id == sandbox.id
        bind.assert_not_called()
        assert restored._require_client() is bind.return_value
        assert restored._require_client() is bind.return_value
        bind.assert_called_once_with()
    creator.assert_not_called()


def test_generated_worker_supports_sandbox_and_empty_lists():
    namespace = {}
    exec(handler_runtime_source(), namespace)
    sandbox = Sandbox(sandbox_id="sandbox-123")
    encoded = namespace["_chalk_encoder"](Sandbox)(sandbox)
    assert namespace["_chalk_decoder"](Sandbox)(encoded).id == sandbox.id
    for annotation in [list[Sandbox], list[list[Sandbox]]]:
        value = arrow_encoder_for_annotation(annotation)([])
        assert arrow_decoder_for_annotation(annotation)(value) == []
        assert namespace["_chalk_encoder"](annotation)([]).type == value.type


def test_unstarted_sandbox_cannot_be_returned():
    with pytest.raises(SandboxError, match="not running"):
        Sandbox(image="ubuntu:latest").__chalk_serialize__()


@pytest.mark.parametrize(
    "value",
    [
        pa.scalar(None, type=Sandbox.__chalk_pyarrow_type__()),
        pa.scalar({"sandbox_id": None}, type=Sandbox.__chalk_pyarrow_type__()),
        pa.scalar({"sandbox_id": ""}, type=Sandbox.__chalk_pyarrow_type__()),
        pa.scalar("sandbox-123"),
        pa.scalar({"sandbox_id": "123", "secret": "unexpected"}),
    ],
)
def test_invalid_handle_is_rejected_without_auth(value):
    with patch("chalkcompute._sandbox.SandboxClient.from_env") as bind:
        with pytest.raises(ValueError):
            Sandbox.__chalk_deserialize__(value)
        bind.assert_not_called()


def test_catalog_preserves_type_identity_description_and_defaults():
    from chalkcompute._function import _build_schemas

    def provision(project: str = "demo", limit: int = 2) -> Sandbox:
        """Prepare a sandbox for data science."""
        raise NotImplementedError

    inputs, outputs = _build_schemas(provision)
    assert inputs.columns[0].metadata["chalk.default"] == '"demo"'
    assert inputs.columns[1].metadata["chalk.default"] == "2"
    assert outputs.columns[0].metadata["chalk.python.type"] == "chalkcompute.Sandbox"
    assert not outputs.columns[0].nullable
    assert outputs.metadata["chalk.function.description"] == provision.__doc__


def test_catalog_distinguishes_optional_list_and_generator_sandboxes():
    from typing import Iterator
    from chalkcompute._arrow_schema import rich_to_arrow_schema
    from chalkcompute._function import _build_schemas

    optional = rich_to_arrow_schema({"result": Sandbox | None}).columns[0]
    assert optional.metadata["chalk.python.type"] == "chalkcompute.Sandbox"
    assert optional.nullable
    listed = rich_to_arrow_schema({"result": list[Sandbox]}).columns[0]
    assert "chalk.python.type" not in listed.metadata

    def generator():
        yield Sandbox(sandbox_id="example")

    generator.__annotations__ = {"return": Iterator[Sandbox]}
    _, output = _build_schemas(generator)
    assert output.metadata["chalk.function.is_generator"] == "true"
