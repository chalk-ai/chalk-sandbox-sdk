"""Optional native integration checks; run with the matching compiler wheel."""

import sys
import types

import pyarrow as pa
import pytest

pytest.importorskip("chalkdf")
pytest.importorskip("static_accelerator.scalar_function")

from chalkcompute._function import _generate_handler_shim


def score(x: float) -> float:
    return x * 2.0


def normalize(x: str | None) -> str:
    if x is None:
        return "missing"
    return x.strip().lower()


def divide(x: float) -> float:
    return 1.0 / x


def load(monkeypatch, function):
    monkeypatch.setenv("CHALK_FUNCTION_STATIC_ACCELERATION", "1")
    module = types.ModuleType("_native_test_user")
    module.function = function
    monkeypatch.setitem(sys.modules, module.__name__, module)
    namespace = {}
    exec(_generate_handler_shim("function", module_name=module.__name__), namespace)
    assert namespace["_accelerated"] is not None
    return namespace


def test_large_batch_does_not_call_python(monkeypatch):
    namespace = load(monkeypatch, score)

    def unexpected(**kwargs):
        pytest.fail("native batch called the Python body")

    namespace["function"] = unexpected
    assert (
        namespace["handler"]({"x": pa.array([2.0] * 1024)}, {}).to_pylist()
        == [4.0] * 1024
    )


def test_nullable_batch_and_unicode_fallback(monkeypatch):
    namespace = load(monkeypatch, normalize)
    assert (
        namespace["handler"]({"x": pa.array([None, " A "] * 512)}, {}).to_pylist()
        == ["missing", "a"] * 512
    )
    assert (
        namespace["handler"]({"x": pa.array(["İ", "ß"] * 512)}, {}) == ["i̇", "ß"] * 512
    )


def test_original_python_exception_survives_native_failure(monkeypatch):
    namespace = load(monkeypatch, divide)
    with pytest.raises(ZeroDivisionError):
        namespace["handler"]({"x": pa.array([0.0] * 1024)}, {})
