from dataclasses import dataclass
from typing import Annotated, Iterator

import pytest

import chalkcompute as cc
from chalkcompute._capture import CAPTURE_OPTION, SECRET_METADATA
from chalkcompute._call_context import reset_call_scope, set_call_scope
from chalkcompute._function import RemoteFunction, _build_schemas


@dataclass
class Credentials:
    token: Annotated[str, cc.Secret]
    name: str


def example(x: int, token: Annotated[str, cc.Secret] = "do-not-persist") -> int:
    return x


def test_policy_serialized_and_secret_default_excluded(no_deploy):
    fn = RemoteFunction(example, capture=cc.CapturePolicy(sample_rate=0.25))
    assert '"sample_rate":0.25' in fn._options[CAPTURE_OPTION]
    inputs, _ = _build_schemas(example)
    assert inputs.columns[1].metadata[SECRET_METADATA] == "true"
    assert "chalk.default" not in inputs.columns[1].metadata
    assert "do-not-persist" not in repr(inputs)


def test_nested_secret_excludes_enclosing_argument():
    def f(value: list[Credentials]) -> Credentials:
        return value[0]

    inputs, outputs = _build_schemas(f)
    assert inputs.columns[0].metadata[SECRET_METADATA] == "true"
    assert outputs.columns[0].metadata[SECRET_METADATA] == "true"


def test_generator_secret_yield_is_preserved():
    def f() -> Iterator[Annotated[str, cc.Secret]]:
        yield "secret"

    _, outputs = _build_schemas(f)
    assert outputs.columns[0].metadata[SECRET_METADATA] == "true"


@pytest.mark.parametrize("rate", [-1, 2, float("nan"), float("inf")])
def test_invalid_rates(rate):
    with pytest.raises(ValueError):
        cc.CapturePolicy(sample_rate=rate)


def test_invalid_boolean_rate():
    with pytest.raises(TypeError):
        cc.CapturePolicy(sample_rate=True)


def test_call_overrides_are_isolated_and_inherited_denial_is_sticky(no_deploy):
    fn = RemoteFunction(example)
    quiet = fn.with_capture(inputs=False)
    loud = fn.with_capture(inputs=True)
    assert ("x-chalk-capture-inputs", "false") in quiet._per_call_metadata()
    assert ("x-chalk-capture-inputs", "true") in loud._per_call_metadata()
    assert "x-chalk-capture-inputs" not in dict(fn._per_call_metadata())
    token = set_call_scope({"metadata": {"x-chalk-capture-inputs": "false"}}, None)
    try:
        assert ("x-chalk-capture-inputs", "false") in loud._per_call_metadata()
    finally:
        reset_call_scope(token)
    assert (
        dict(fn._per_call_metadata())["x-chalk-call-id"]
        != dict(fn._per_call_metadata())["x-chalk-call-id"]
    )


def test_capture_false_serialized(no_deploy):
    assert (
        '"enabled":false'
        in RemoteFunction(example, capture=False)._options[CAPTURE_OPTION]
    )


def test_reserved_option_rejected(no_deploy):
    with pytest.raises(ValueError, match="reserved"):
        RemoteFunction(example, options={CAPTURE_OPTION: "{}"})


def test_strip_mode_preserves_secret_annotations_without_sdk_import():
    from chalkcompute._function import _strip_chalkcompute_from_source

    source = """from typing import Annotated
import chalkcompute as cc
@cc.function(capture=True)
def score(token: Annotated[str, cc.Secret]) -> int:
    return 1
"""
    namespace = {}
    exec(_strip_chalkcompute_from_source(source), namespace)
    from typing import get_type_hints

    assert get_type_hints(namespace["score"])["token"] is str


def test_capture_requires_compatible_runtime(no_deploy, monkeypatch):
    import chalk_remote_call

    monkeypatch.setattr(chalk_remote_call, "CAPTURE_PROTOCOL_VERSION", 0, raising=False)
    with pytest.raises(RuntimeError, match="capture protocol"):
        RemoteFunction(example, capture=True)._remote_call_runtime_package()


def test_class_methods_preserve_secret_input_and_output_annotations():
    from chalkcompute._cls import _build_method_schemas

    class Worker:
        def run(self, value: Annotated[str, cc.Secret]) -> Annotated[int, cc.Secret]:
            return len(value)

    inputs, outputs = _build_method_schemas(Worker.run)
    assert inputs.columns[0].metadata[SECRET_METADATA] == "true"
    assert outputs.columns[0].metadata[SECRET_METADATA] == "true"


def test_class_strip_mode_preserves_secret_annotations():
    from chalkcompute._cls import _strip_chalkcompute_from_class_source

    source = """from typing import Annotated
import chalkcompute as cc
@cc.cls(capture=True)
class Worker:
    @cc.method()
    def run(self, token: Annotated[str, cc.Secret]) -> int:
        return 1
"""
    namespace = {}
    exec(_strip_chalkcompute_from_class_source(source), namespace)
    from typing import get_type_hints

    assert get_type_hints(namespace["Worker"].run)["token"] is str
