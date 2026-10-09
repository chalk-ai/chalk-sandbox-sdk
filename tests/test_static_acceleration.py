"""Generated-handler wiring; native equivalence lives with static_accelerator."""

import functools
import sys
import types

import pyarrow as pa
import pytest

from chalkcompute._function import _NoOpFunction, _generate_handler_shim


def score(x: float) -> float:
    return x * 2.0


@pytest.fixture
def compiler(monkeypatch):
    calls = []

    class Compiled:
        def accepts(self, inputs):
            return inputs["x"].type == pa.float64() and inputs["x"].null_count == 0

        def __call__(self, inputs):
            calls.append(inputs)
            return pa.array([score(x) for x in inputs["x"].to_pylist()])

    module = types.ModuleType("static_accelerator.scalar_function")
    module.compile_scalar_function = lambda function: Compiled()
    monkeypatch.setitem(sys.modules, "static_accelerator.scalar_function", module)
    # Native equivalence is exercised separately; the handler must load it only
    # when requested, including when it is absent from a lightweight image.
    monkeypatch.setitem(sys.modules, "chalkdf", types.ModuleType("chalkdf"))
    monkeypatch.setenv("CHALK_FUNCTION_STATIC_ACCELERATION", "1")
    return module, calls


def load_handler(monkeypatch, function):
    module = types.ModuleType("user_module")
    module.score = function
    monkeypatch.setitem(sys.modules, "user_module", module)
    namespace = {}
    exec(_generate_handler_shim("score"), namespace)
    return namespace


def test_native_handler_returns_arrow_and_ignores_reserved_metadata(
    monkeypatch, compiler
):
    _, calls = compiler
    namespace = load_handler(monkeypatch, score)
    result = namespace["handler"](
        {
            "x": pa.array([2.0] * 1024),
            "__chalk_row_metadata__": pa.array([b"metadata"] * 1024),
        },
        {"metadata": {"session": "test"}},
    )
    assert result.to_pylist() == [4.0] * 1024
    assert list(calls[0]) == ["x"]


def test_opt_in_and_compiler_failure_preserve_python(monkeypatch, compiler):
    module, calls = compiler
    monkeypatch.delenv("CHALK_FUNCTION_STATIC_ACCELERATION")
    namespace = load_handler(monkeypatch, score)
    assert namespace["_accelerated"] is None
    assert namespace["handler"]({"x": pa.array([2.0])}, {}) == [4.0]
    assert not calls
    monkeypatch.setenv("CHALK_FUNCTION_STATIC_ACCELERATION", "1")

    def fail(function):
        raise ValueError("unsupported")

    module.compile_scalar_function = fail
    namespace = load_handler(monkeypatch, score)
    assert namespace["_accelerated"] is None
    assert namespace["handler"]({"x": pa.array([2.0])}, {}) == [4.0]


def test_runtime_failure_retries_pure_function_in_python(monkeypatch, compiler):
    namespace = load_handler(monkeypatch, score)

    class Fails:
        def accepts(self, inputs):
            return True

        def __call__(self, inputs):
            raise ValueError("native failure")

    namespace["_accelerated"] = Fails()
    assert namespace["handler"]({"x": pa.array([2.0] * 1024)}, {}) == [4.0] * 1024


def test_only_sdk_wrapper_is_removed(monkeypatch, compiler):
    module, _ = compiler
    seen = []
    module.compile_scalar_function = lambda function: seen.append(function)
    load_handler(monkeypatch, _NoOpFunction(score))
    assert seen == [score]

    @functools.wraps(score)
    def decorated(x):
        return score(x) + 1

    load_handler(monkeypatch, decorated)
    assert seen[-1] is decorated


def test_incompatible_schema_keeps_python(monkeypatch, compiler):
    _, calls = compiler
    namespace = load_handler(monkeypatch, score)
    assert namespace["handler"]({"x": pa.array([2] * 1024)}, {}) == [4.0] * 1024
    assert not calls


def test_small_batches_keep_python(monkeypatch, compiler):
    _, calls = compiler
    namespace = load_handler(monkeypatch, score)
    assert namespace["handler"]({"x": pa.array([2.0])}, {}) == [4.0]
    assert not calls
