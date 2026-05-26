"""Tests for the chalk-remote-call handler shims emitted by _function and _cls."""

from __future__ import annotations

import asyncio
import sys
import types

import pyarrow as pa
import pytest
from chalk_remote_call.arrow_utils import decode_ipc_stream, encode_record_batch

from chalkcompute._cls import _generate_class_handler_shim
from chalkcompute._function import _generate_handler_shim


def _install_user_module(**attrs) -> None:
    mod = types.ModuleType("user_module")
    for name, value in attrs.items():
        setattr(mod, name, value)
    sys.modules["user_module"] = mod


def _load_shim(source: str) -> dict:
    ns: dict = {}
    exec(compile(source, "<shim>", "exec"), ns)
    return ns


def _encode_event(event: dict[str, pa.Array]) -> bytes:
    """Serialize a dict-of-arrays into an IPC stream via chalk-remote-call's encoder."""
    return encode_record_batch(
        pa.record_batch(list(event.values()), names=list(event.keys()))
    )


def _decode_result_column(payloads: list[bytes]) -> list[list]:
    """Decode each response payload and extract its ``result`` column as a pylist."""
    out: list[list] = []
    for payload in payloads:
        for batch in decode_ipc_stream(payload):
            out.append(batch.column("result").to_pylist())
    return out


class TestGenerateHandlerShimNormal:
    def test_signature(self):
        src = _generate_handler_shim("fn")
        assert "def handler(event, context):" in src

    def test_sync_scalar(self):
        def add(x: int, y: int) -> int:
            return x + y

        _install_user_module(add=add)
        ns = _load_shim(_generate_handler_shim("add"))
        event = {"x": pa.array([1, 2, 3]), "y": pa.array([10, 20, 30])}
        assert ns["handler"](event, {}) == [11, 22, 33]

    def test_sync_generator_streams_per_yield(self):
        def expand(n: int):
            for i in range(n):
                yield i

        _install_user_module(expand=expand)
        ns = _load_shim(_generate_handler_shim("expand"))
        event = {"n": pa.array([2, 3])}
        assert list(ns["handler"](event, {})) == [0, 1, 0, 1, 2]

    def test_async_coroutine(self):
        async def add(x: int, y: int) -> int:
            return x + y

        _install_user_module(add=add)
        ns = _load_shim(_generate_handler_shim("add"))
        event = {"x": pa.array([1, 2]), "y": pa.array([10, 20])}
        assert ns["handler"](event, {}) == [11, 22]

    def test_async_generator_streams_per_yield(self):
        async def expand(n: int):
            for i in range(n):
                yield i

        _install_user_module(expand=expand)
        ns = _load_shim(_generate_handler_shim("expand"))
        event = {"n": pa.array([2, 3])}
        agen = ns["handler"](event, {})

        async def _collect():
            return [item async for item in agen]

        assert asyncio.run(_collect()) == [0, 1, 0, 1, 2]


class TestGenerateHandlerShimBatched:
    def test_signature(self):
        src = _generate_handler_shim("fn", batched=True)
        assert "def handler(events, contexts):" in src
        assert "_flatten_events(events)" in src
        assert "as_completed" in src

    def test_sync_multi_caller_returns_list_of_lists(self):
        def add(x: int, y: int) -> int:
            return x + y

        _install_user_module(add=add)
        ns = _load_shim(_generate_handler_shim("add", batched=True))
        events = [
            {"x": pa.array([1, 2]), "y": pa.array([10, 20])},
            {"x": pa.array([3]), "y": pa.array([30])},
        ]
        assert ns["handler"](events, [{}, {}]) == [[11, 22], [33]]

    def test_sync_single_caller(self):
        def double(x: int) -> int:
            return x * 2

        _install_user_module(double=double)
        ns = _load_shim(_generate_handler_shim("double", batched=True))
        events = [{"x": pa.array([5, 6, 7])}]
        assert ns["handler"](events, [{}]) == [[10, 12, 14]]

    def test_async_coroutine_multi_caller(self):
        async def add(x: int, y: int) -> int:
            return x + y

        _install_user_module(add=add)
        ns = _load_shim(_generate_handler_shim("add", batched=True))
        events = [
            {"x": pa.array([1]), "y": pa.array([2])},
            {"x": pa.array([10, 20]), "y": pa.array([100, 200])},
        ]
        assert ns["handler"](events, [{}, {}]) == [[3], [110, 220]]

    def test_async_coroutine_preserves_order_with_different_row_counts(self):
        seen: list[tuple[int, int]] = []

        async def add(x: int, y: int) -> int:
            seen.append((x, y))
            return x + y

        _install_user_module(add=add)
        ns = _load_shim(_generate_handler_shim("add", batched=True))
        events = [
            {"x": pa.array([1, 2, 3]), "y": pa.array([10, 20, 30])},
            {"x": pa.array([4]), "y": pa.array([40])},
        ]
        assert ns["handler"](events, [{}, {}]) == [[11, 22, 33], [44]]
        assert seen == [(1, 10), (2, 20), (3, 30), (4, 40)]

    def test_empty_events_list(self):
        def double(x: int) -> int:
            return x * 2

        _install_user_module(double=double)
        ns = _load_shim(_generate_handler_shim("double", batched=True))
        assert ns["handler"]([], []) == []

    def test_async_coroutine_uses_single_asyncio_run_per_coalesced_call(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        run_count = 0
        original_run = asyncio.run

        def counting_run(coro):
            nonlocal run_count
            run_count += 1
            return original_run(coro)

        monkeypatch.setattr(asyncio, "run", counting_run)

        async def add(x: int, y: int) -> int:
            return x + y

        _install_user_module(add=add)
        ns = _load_shim(_generate_handler_shim("add", batched=True))
        events = [
            {"x": pa.array([1]), "y": pa.array([2])},
            {"x": pa.array([10, 20]), "y": pa.array([100, 200])},
        ]

        assert ns["handler"](events, [{}, {}]) == [[3], [110, 220]]
        assert run_count == 1


class TestGenerateClassHandlerShimNormal:
    def test_signature(self):
        src = _generate_class_handler_shim(
            "Adder", "add", True, None, False, None, False
        )
        assert "def handler(event, context):" in src

    def test_method_with_self(self):
        class Adder:
            def __init__(self):
                self.offset = 100

            def add(self, x: int, y: int) -> int:
                return self.offset + x + y

        _install_user_module(Adder=Adder)
        src = _generate_class_handler_shim(
            "Adder", "add", True, None, False, None, False
        )
        ns = _load_shim(src)
        event = {"x": pa.array([1, 2]), "y": pa.array([10, 20])}
        assert ns["handler"](event, {}) == [111, 122]

    def test_method_without_self(self):
        class Calc:
            @staticmethod
            def double(x: int) -> int:
                return x * 2

        _install_user_module(Calc=Calc)
        src = _generate_class_handler_shim(
            "Calc", "double", False, None, False, None, False
        )
        ns = _load_shim(src)
        event = {"x": pa.array([3, 4, 5])}
        assert ns["handler"](event, {}) == [6, 8, 10]

    def test_async_coroutine_method(self):
        class Mul:
            async def mul(self, x: int, y: int) -> int:
                return x * y

        _install_user_module(Mul=Mul)
        src = _generate_class_handler_shim(
            "Mul", "mul", True, None, False, None, False
        )
        ns = _load_shim(src)
        event = {"x": pa.array([2, 3]), "y": pa.array([10, 10])}
        assert ns["handler"](event, {}) == [20, 30]

    def test_async_staticmethod_no_self(self):
        class Calc:
            @staticmethod
            async def double(x: int) -> int:
                return x * 2

        _install_user_module(Calc=Calc)
        src = _generate_class_handler_shim(
            "Calc", "double", False, None, False, None, False
        )
        ns = _load_shim(src)
        event = {"x": pa.array([4, 5])}
        assert ns["handler"](event, {}) == [8, 10]

    def test_sync_generator_method(self):
        class G:
            def expand(self, n: int):
                for i in range(n):
                    yield i

        _install_user_module(G=G)
        src = _generate_class_handler_shim(
            "G", "expand", True, None, False, None, False
        )
        ns = _load_shim(src)
        assert list(ns["handler"]({"n": pa.array([2, 3])}, {})) == [0, 1, 0, 1, 2]

    def test_before_hook_is_defined_as_on_startup(self):
        class Stateful:
            def __init__(self):
                self.offset = 0

            def warmup(self):
                self.offset = 100

            def value(self, x: int) -> int:
                return self.offset + x

        _install_user_module(Stateful=Stateful)
        src = _generate_class_handler_shim(
            "Stateful", "value", True, "warmup", True, None, False
        )
        ns = _load_shim(src)
        event = {"x": pa.array([1])}
        assert callable(ns["on_startup"])
        assert ns["handler"](event, {}) == [1]
        ns["on_startup"]()
        assert ns["handler"](event, {}) == [101]


class TestGenerateClassHandlerShimBatched:
    def test_signature(self):
        src = _generate_class_handler_shim(
            "Adder", "add", True, None, False, None, False, batched=True
        )
        assert "def handler(events, contexts):" in src
        assert "for event in events:" in src
        assert "_is_coroutine" in src

    def test_sync_multi_caller(self):
        class Adder:
            def add(self, x: int, y: int) -> int:
                return x + y

        _install_user_module(Adder=Adder)
        src = _generate_class_handler_shim(
            "Adder", "add", True, None, False, None, False, batched=True
        )
        ns = _load_shim(src)
        events = [
            {"x": pa.array([1, 2]), "y": pa.array([10, 20])},
            {"x": pa.array([3]), "y": pa.array([30])},
        ]
        assert ns["handler"](events, [{}, {}]) == [[11, 22], [33]]

    def test_async_coroutine_multi_caller(self):
        class Mul:
            async def mul(self, x: int, y: int) -> int:
                return x * y

        _install_user_module(Mul=Mul)
        src = _generate_class_handler_shim(
            "Mul", "mul", True, None, False, None, False, batched=True
        )
        ns = _load_shim(src)
        events = [
            {"x": pa.array([2]), "y": pa.array([3])},
            {"x": pa.array([4, 5]), "y": pa.array([10, 10])},
        ]
        assert ns["handler"](events, [{}, {}]) == [[6], [40, 50]]

    def test_async_staticmethod_no_self(self):
        class Calc:
            @staticmethod
            async def double(x: int) -> int:
                return x * 2

        _install_user_module(Calc=Calc)
        src = _generate_class_handler_shim(
            "Calc", "double", False, None, False, None, False, batched=True
        )
        ns = _load_shim(src)
        events = [
            {"x": pa.array([3, 4])},
            {"x": pa.array([5])},
        ]
        assert ns["handler"](events, [{}, {}]) == [[6, 8], [10]]

    def test_matches_function_shim_behavior(self):
        def add(x: int, y: int) -> int:
            return x + y

        class Adder:
            def add(self, x: int, y: int) -> int:
                return x + y

        _install_user_module(add=add, Adder=Adder)
        function_ns = _load_shim(_generate_handler_shim("add", batched=True))
        class_ns = _load_shim(
            _generate_class_handler_shim(
                "Adder", "add", True, None, False, None, False, batched=True
            )
        )
        events = [
            {"x": pa.array([1, 2]), "y": pa.array([10, 20])},
            {"x": pa.array([3]), "y": pa.array([30])},
        ]
        contexts = [{"caller": "a"}, {"caller": "b"}]

        assert class_ns["handler"](events, contexts) == function_ns["handler"](
            events, contexts
        )


class TestShimViaRemoteCallLib:
    """End-to-end: drive chalk_remote_call.servicer.process_batches with our shim.

    This is the real runtime path that Rust invokes per request in prod —
    IPC-decode → transform → handler(event, context) → coerce → IPC-encode.
    Any schema/shape mismatch between our shim and chalk-remote-call would
    surface here.
    """

    def test_sync_function_roundtrip(self):
        from chalk_remote_call.servicer import process_batches

        def add(x: int, y: int) -> int:
            return x + y

        _install_user_module(add=add)
        ns = _load_shim(_generate_handler_shim("add"))

        ipc_bytes = _encode_event({"x": pa.array([1, 2, 3]), "y": pa.array([10, 20, 30])})
        payloads = process_batches(ipc_bytes, ns["handler"], None, {})
        assert _decode_result_column(payloads) == [[11, 22, 33]]

    def test_async_function_roundtrip(self):
        from chalk_remote_call.servicer import process_batches

        async def add(x: int, y: int) -> int:
            return x + y

        _install_user_module(add=add)
        ns = _load_shim(_generate_handler_shim("add"))

        ipc_bytes = _encode_event({"x": pa.array([1, 2]), "y": pa.array([10, 20])})
        payloads = process_batches(ipc_bytes, ns["handler"], None, {})
        assert _decode_result_column(payloads) == [[11, 22]]

    def test_sync_generator_streams_per_yield(self):
        from chalk_remote_call.servicer import process_batches

        def expand(n: int):
            for i in range(n):
                yield i

        _install_user_module(expand=expand)
        ns = _load_shim(_generate_handler_shim("expand"))

        ipc_bytes = _encode_event({"n": pa.array([2, 3])})
        payloads = process_batches(ipc_bytes, ns["handler"], None, {})
        # Each yielded value is encoded as a separate chunk.
        assert _decode_result_column(payloads) == [[0], [1], [0], [1], [2]]

    def test_class_method_roundtrip(self):
        from chalk_remote_call.servicer import process_batches

        class Adder:
            def __init__(self):
                self.offset = 100

            def add(self, x: int, y: int) -> int:
                return self.offset + x + y

        _install_user_module(Adder=Adder)
        src = _generate_class_handler_shim(
            "Adder", "add", True, None, False, None, False
        )
        ns = _load_shim(src)

        ipc_bytes = _encode_event({"x": pa.array([1, 2]), "y": pa.array([10, 20])})
        payloads = process_batches(ipc_bytes, ns["handler"], None, {})
        assert _decode_result_column(payloads) == [[111, 122]]

    def test_batched_function_roundtrip_via_coalesced_bridge(self):
        from chalk_remote_call.servicer import process_batches_coalesced

        def add(x: int, y: int) -> int:
            return x + y

        _install_user_module(add=add)
        ns = _load_shim(_generate_handler_shim("add", batched=True))

        payloads = process_batches_coalesced(
            [
                _encode_event({"x": pa.array([1, 2]), "y": pa.array([10, 20])}),
                _encode_event({"x": pa.array([3]), "y": pa.array([30])}),
            ],
            ns["handler"],
            None,
            [{}, {}],
        )
        assert _decode_result_column(payloads) == [[11, 22], [33]]

    def test_batched_class_roundtrip_via_coalesced_bridge(self):
        from chalk_remote_call.servicer import process_batches_coalesced

        class Adder:
            def add(self, x: int, y: int) -> int:
                return x + y

        _install_user_module(Adder=Adder)
        ns = _load_shim(
            _generate_class_handler_shim(
                "Adder", "add", True, None, False, None, False, batched=True
            )
        )

        payloads = process_batches_coalesced(
            [
                _encode_event({"x": pa.array([1]), "y": pa.array([10])}),
                _encode_event({"x": pa.array([2, 3]), "y": pa.array([20, 30])}),
            ],
            ns["handler"],
            None,
            [{}, {}],
        )
        assert _decode_result_column(payloads) == [[11], [22, 33]]
