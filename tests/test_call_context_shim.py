"""The handler shims publish the caller's context and per-row metadata as context variables.

Two side channels, one mechanism: the request's gRPC headers arrive as the handler's
``context`` argument (per request), and per-row metadata arrives as a reserved column (per
row). Neither may reach the user function as an argument — the point of both is that a
function reads them without declaring a dependency — and both are optional in both
directions: a caller may send neither, and a function may read neither.
"""

from __future__ import annotations

import asyncio
import builtins
import sys
import types

import pyarrow as pa
import pytest

from chalkcompute._call_context import get_call_context, get_row_metadata
from chalkcompute._cls import _generate_class_handler_shim
from chalkcompute._function import _ROW_METADATA_COLUMN, _generate_handler_shim

_USER_MODULE = "user_module"


@pytest.fixture(autouse=True)
def _restore_user_module():
    """Undo the fake ``user_module`` these tests install.

    A bare ModuleType has no ``__spec__``, so leaving one in ``sys.modules`` makes
    ``importlib.util.find_spec("user_module")`` raise for every later test in the session —
    which is exactly what ``add_local_python_source`` calls.
    """
    previous = sys.modules.get(_USER_MODULE)
    try:
        yield
    finally:
        if previous is None:
            sys.modules.pop(_USER_MODULE, None)
        else:
            sys.modules[_USER_MODULE] = previous


def _install_user_module(**attrs) -> None:
    mod = types.ModuleType(_USER_MODULE)
    for name, value in attrs.items():
        setattr(mod, name, value)
    sys.modules[_USER_MODULE] = mod


def _load_shim(source: str) -> dict:
    ns: dict = {}
    exec(compile(source, "<shim>", "exec"), ns)
    return ns


def _row_metadata_column(values: list[bytes | None]) -> pa.Array:
    return pa.array(values, pa.binary())


class TestNonBatchedShim:
    def test_row_metadata_is_visible_and_not_an_argument(self):
        seen: list[bytes | None] = []

        def echo(x: int) -> int:
            seen.append(get_row_metadata())
            return x

        _install_user_module(echo=echo)
        ns = _load_shim(_generate_handler_shim("echo"))
        event = {
            "x": pa.array([1, 2]),
            _ROW_METADATA_COLUMN: _row_metadata_column([b"s-1", b"s-2"]),
        }

        assert ns["handler"](event, {}) == [1, 2]
        assert seen == [b"s-1", b"s-2"]

    def test_call_context_is_visible_and_not_an_argument(self):
        seen: list[dict[str, str]] = []

        def echo(x: int) -> int:
            seen.append(dict(get_call_context()))
            return x

        _install_user_module(echo=echo)
        ns = _load_shim(_generate_handler_shim("echo"))
        context = {"x-chalk-session-id": "s-1", "traceparent": "00-abc-def-01"}

        assert ns["handler"]({"x": pa.array([1, 2])}, context) == [1, 2]
        assert seen == [context, context]

    def test_both_channels_together(self):
        seen: list[tuple[str | None, bytes | None]] = []

        def echo(x: int) -> int:
            seen.append(
                (get_call_context().get("x-chalk-session-id"), get_row_metadata())
            )
            return x

        _install_user_module(echo=echo)
        ns = _load_shim(_generate_handler_shim("echo"))
        event = {
            "x": pa.array([1, 2]),
            _ROW_METADATA_COLUMN: _row_metadata_column([b"a", b"b"]),
        }

        ns["handler"](event, {"x-chalk-session-id": "s-1"})
        assert seen == [("s-1", b"a"), ("s-1", b"b")]

    def test_a_caller_may_send_neither(self):
        seen: list[tuple[dict[str, str], bytes | None]] = []

        def echo(x: int) -> int:
            seen.append((dict(get_call_context()), get_row_metadata()))
            return x

        _install_user_module(echo=echo)
        ns = _load_shim(_generate_handler_shim("echo"))

        assert ns["handler"]({"x": pa.array([1])}, {}) == [1]
        assert seen == [({}, None)]

    def test_a_function_may_read_neither(self):
        # The reserved column must not leak into the argument mapping even when nothing
        # reads it, or the call fails with an unexpected keyword argument.
        def echo(x: int) -> int:
            return x

        _install_user_module(echo=echo)
        ns = _load_shim(_generate_handler_shim("echo"))
        event = {
            "x": pa.array([1, 2]),
            _ROW_METADATA_COLUMN: _row_metadata_column([b"s-1", b"s-2"]),
        }

        assert ns["handler"](event, {"x-chalk-session-id": "s-1"}) == [1, 2]

    def test_null_row_metadata_reads_as_none(self):
        seen: list[bytes | None] = []

        def echo(x: int) -> int:
            seen.append(get_row_metadata())
            return x

        _install_user_module(echo=echo)
        ns = _load_shim(_generate_handler_shim("echo"))
        event = {
            "x": pa.array([1, 2]),
            _ROW_METADATA_COLUMN: _row_metadata_column([None, b"s-2"]),
        }

        assert ns["handler"](event, {}) == [1, 2]
        assert seen == [None, b"s-2"]

    def test_both_are_reset_after_the_call(self):
        def echo(x: int) -> int:
            return x

        _install_user_module(echo=echo)
        ns = _load_shim(_generate_handler_shim("echo"))
        event = {
            "x": pa.array([1]),
            _ROW_METADATA_COLUMN: _row_metadata_column([b"s-1"]),
        }
        ns["handler"](event, {"x-chalk-session-id": "s-1"})

        assert get_row_metadata() is None
        assert get_call_context() == {}

    def test_the_context_is_read_only(self):
        def echo(x: int) -> int:
            try:
                get_call_context()["injected"] = "nope"  # ty: ignore[unsupported-operation]
            except TypeError:
                return 1
            return 0

        _install_user_module(echo=echo)
        ns = _load_shim(_generate_handler_shim("echo"))

        assert ns["handler"]({"x": pa.array([1])}, {"a": "b"}) == [1]

    def test_generator(self):
        def expand(n: int):
            for _ in range(n):
                yield (get_call_context().get("k"), get_row_metadata())

        _install_user_module(expand=expand)
        ns = _load_shim(_generate_handler_shim("expand"))
        event = {
            "n": pa.array([2, 1]),
            _ROW_METADATA_COLUMN: _row_metadata_column([b"s-1", b"s-2"]),
        }

        assert list(ns["handler"](event, {"k": "v"})) == [
            ("v", b"s-1"),
            ("v", b"s-1"),
            ("v", b"s-2"),
        ]

    def test_coroutine(self):
        async def echo(x: int) -> tuple[str | None, bytes | None]:
            return (get_call_context().get("k"), get_row_metadata())

        _install_user_module(echo=echo)
        ns = _load_shim(_generate_handler_shim("echo"))
        event = {
            "x": pa.array([1, 2]),
            _ROW_METADATA_COLUMN: _row_metadata_column([b"s-1", b"s-2"]),
        }

        assert ns["handler"](event, {"k": "v"}) == [("v", b"s-1"), ("v", b"s-2")]

    def test_async_generator(self):
        async def expand(n: int):
            for _ in range(n):
                yield (get_call_context().get("k"), get_row_metadata())

        _install_user_module(expand=expand)
        ns = _load_shim(_generate_handler_shim("expand"))
        event = {
            "n": pa.array([1, 2]),
            _ROW_METADATA_COLUMN: _row_metadata_column([b"s-1", b"s-2"]),
        }
        agen = ns["handler"](event, {"k": "v"})

        async def _collect():
            return [item async for item in agen]

        assert asyncio.run(_collect()) == [("v", b"s-1"), ("v", b"s-2"), ("v", b"s-2")]


class TestWithoutChalkcomputeInstalled:
    """Deploy images built in strip mode ship only the remote-call runtime.

    The shim must still run there. The context is never empty in production — the runtime
    always sends authorization/env/trace headers — so the "only import when needed" guard
    does not save us; the import itself has to be optional.
    """

    @staticmethod
    def _block_chalkcompute(monkeypatch) -> None:
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("chalkcompute"):
                raise ImportError(f"No module named {name!r}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        # Without this the tests below would pass whether or not the shim guards its import.
        with pytest.raises(ImportError):
            builtins.__import__("chalkcompute._call_context")

    def test_non_batched_shim_runs(self, monkeypatch):
        def echo(x: int) -> int:
            return x

        _install_user_module(echo=echo)
        ns = _load_shim(_generate_handler_shim("echo"))
        event = {
            "x": pa.array([1, 2]),
            _ROW_METADATA_COLUMN: _row_metadata_column([b"s-1", b"s-2"]),
        }
        self._block_chalkcompute(monkeypatch)

        assert ns["handler"](event, {"authorization": "Bearer x"}) == [1, 2]

    def test_batched_shim_runs(self, monkeypatch):
        def echo(x: int) -> int:
            return x

        _install_user_module(echo=echo)
        ns = _load_shim(_generate_handler_shim("echo", batched=True))
        events = [{"x": pa.array([1, 2])}]
        self._block_chalkcompute(monkeypatch)

        assert ns["handler"](events, [{"authorization": "Bearer x"}]) == [[1, 2]]

    def test_class_shim_runs(self, monkeypatch):
        class Svc:
            def run(self, x: int) -> int:
                return x

        _install_user_module(Svc=Svc)
        ns = _load_shim(
            _generate_class_handler_shim(
                class_name="Svc",
                method_name="run",
                method_has_self=True,
                before_name=None,
                before_has_self=False,
                after_name=None,
                after_has_self=False,
            )
        )
        self._block_chalkcompute(monkeypatch)

        assert ns["handler"]({"x": pa.array([1])}, {"authorization": "Bearer x"}) == [1]


class TestBatchedShim:
    def test_each_row_sees_its_own_metadata_across_threads(self):
        # The batched shim runs rows on a thread pool, where a context variable set by the
        # caller would not be visible; each work item carries its own values instead.
        def echo(x: int) -> tuple[int, bytes | None]:
            return (x, get_row_metadata())

        _install_user_module(echo=echo)
        ns = _load_shim(_generate_handler_shim("echo", batched=True))
        events = [
            {
                "x": pa.array([1, 2]),
                _ROW_METADATA_COLUMN: _row_metadata_column([b"s-1", b"s-2"]),
            },
            {
                "x": pa.array([3]),
                _ROW_METADATA_COLUMN: _row_metadata_column([b"s-3"]),
            },
        ]

        assert ns["handler"](events, [{}, {}]) == [
            [(1, b"s-1"), (2, b"s-2")],
            [(3, b"s-3")],
        ]

    def test_each_caller_sees_its_own_context(self):
        # Coalescing merges several callers into one handler call, so the context is per
        # caller, not per handler invocation.
        def echo(x: int) -> tuple[int, str | None]:
            return (x, get_call_context().get("caller"))

        _install_user_module(echo=echo)
        ns = _load_shim(_generate_handler_shim("echo", batched=True))
        events = [{"x": pa.array([1, 2])}, {"x": pa.array([3])}]

        assert ns["handler"](events, [{"caller": "a"}, {"caller": "b"}]) == [
            [(1, "a"), (2, "a")],
            [(3, "b")],
        ]

    def test_callers_without_either_channel_are_unaffected(self):
        def echo(x: int) -> tuple[int, dict[str, str], bytes | None]:
            return (x, dict(get_call_context()), get_row_metadata())

        _install_user_module(echo=echo)
        ns = _load_shim(_generate_handler_shim("echo", batched=True))
        events = [
            {"x": pa.array([1])},
            {
                "x": pa.array([2]),
                _ROW_METADATA_COLUMN: _row_metadata_column([b"s-2"]),
            },
        ]

        assert ns["handler"](events, [{}, {"caller": "b"}]) == [
            [(1, {}, None)],
            [(2, {"caller": "b"}, b"s-2")],
        ]

    def test_coroutine_rows_are_isolated_per_task(self):
        async def echo(x: int) -> tuple[int, str | None, bytes | None]:
            await asyncio.sleep(0)
            return (x, get_call_context().get("caller"), get_row_metadata())

        _install_user_module(echo=echo)
        ns = _load_shim(_generate_handler_shim("echo", batched=True))
        events = [
            {
                "x": pa.array([1, 2]),
                _ROW_METADATA_COLUMN: _row_metadata_column([b"s-1", b"s-2"]),
            },
            {"x": pa.array([3])},
        ]

        assert ns["handler"](events, [{"caller": "a"}, {"caller": "b"}]) == [
            [(1, "a", b"s-1"), (2, "a", b"s-2")],
            [(3, "b", None)],
        ]


class TestClassShim:
    def _shim(self, *, batched: bool = False) -> dict:
        source = _generate_class_handler_shim(
            class_name="Svc",
            method_name="run",
            method_has_self=True,
            before_name=None,
            before_has_self=False,
            after_name=None,
            after_has_self=False,
            batched=batched,
        )
        return _load_shim(source)

    def test_method_sees_both_channels(self):
        class Svc:
            def run(self, x: int) -> tuple[int, str | None, bytes | None]:
                return (x, get_call_context().get("k"), get_row_metadata())

        _install_user_module(Svc=Svc)
        ns = self._shim()
        event = {
            "x": pa.array([1, 2]),
            _ROW_METADATA_COLUMN: _row_metadata_column([b"s-1", b"s-2"]),
        }

        assert ns["handler"](event, {"k": "v"}) == [(1, "v", b"s-1"), (2, "v", b"s-2")]

    def test_batched_method_sees_both_channels(self):
        class Svc:
            def run(self, x: int) -> tuple[int, str | None, bytes | None]:
                return (x, get_call_context().get("k"), get_row_metadata())

        _install_user_module(Svc=Svc)
        ns = self._shim(batched=True)
        events = [
            {
                "x": pa.array([1]),
                _ROW_METADATA_COLUMN: _row_metadata_column([b"s-1"]),
            }
        ]

        assert ns["handler"](events, [{"k": "v"}]) == [[(1, "v", b"s-1")]]

    def test_method_that_reads_neither(self):
        class Svc:
            def run(self, x: int) -> int:
                return x

        _install_user_module(Svc=Svc)
        ns = self._shim()
        event = {
            "x": pa.array([1]),
            _ROW_METADATA_COLUMN: _row_metadata_column([b"s-1"]),
        }

        assert ns["handler"](event, {"k": "v"}) == [1]
