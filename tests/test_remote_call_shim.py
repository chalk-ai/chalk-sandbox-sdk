"""Tests for the chalk-remote-call handler shims emitted by _function and _cls."""

from __future__ import annotations

import asyncio
import functools
import inspect
import sys
import types
import typing

import pyarrow as pa
import pytest
from protobuf import Oneof
from chalk_remote_call.arrow_utils import decode_ipc_stream, encode_record_batch

from chalkcompute._evaluation import Criterion, Rubric
from chalkcompute._gen.chalk.arrow.v1 import arrow_pb
from chalkcompute._cls import _generate_class_handler_shim
from chalkcompute._evaluation import EvaluationScorerResult
from chalkcompute._function import (
    _NO_ARGS_SENTINEL_COLUMN,
    _NoOpFunction,
    _generate_handler_shim,
    RemoteFunction,
)


_ARROW_POINT_PA_TYPE = pa.struct(
    [
        pa.field("x", pa.int32(), nullable=False),
        pa.field("y", pa.int32(), nullable=False),
    ]
)


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


class ArrowPoint:
    __chalk_arrow_type__ = arrow_pb.ArrowType(
        arrow_type_enum=Oneof(
            "struct",
            arrow_pb.Struct(
                sub_field_types=[
                    arrow_pb.Field(
                        name="x",
                        arrow_type=arrow_pb.ArrowType(
                            arrow_type_enum=Oneof("int32", arrow_pb.EmptyMessage())
                        ),
                        nullable=False,
                    ),
                    arrow_pb.Field(
                        name="y",
                        arrow_type=arrow_pb.ArrowType(
                            arrow_type_enum=Oneof("int32", arrow_pb.EmptyMessage())
                        ),
                        nullable=False,
                    ),
                ],
            ),
        )
    )

    def __init__(self, x: int, y: int):
        self.x = x
        self.y = y

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ArrowPoint) and (self.x, self.y) == (
            other.x,
            other.y,
        )

    def __chalk_serialize__(self) -> pa.Scalar:
        return pa.scalar(
            {"x": self.x, "y": self.y},
            type=_ARROW_POINT_PA_TYPE,
        )

    @classmethod
    def __chalk_deserialize__(cls, value: pa.Scalar) -> ArrowPoint:
        data = value.as_py()
        return cls(data["x"], data["y"])


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

    def test_sync_generator_streams_when_wrapped_by_remote_noop(self):
        def expand(n: int):
            for i in range(n):
                yield i

        _install_user_module(expand=_NoOpFunction(expand))
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

    def test_async_generator_streams_when_wrapped_by_remote_noop(self):
        async def expand(n: int):
            for i in range(n):
                yield i

        _install_user_module(expand=_NoOpFunction(expand))
        ns = _load_shim(_generate_handler_shim("expand"))
        event = {"n": pa.array([2, 3])}
        agen = ns["handler"](event, {})

        async def _collect():
            return [item async for item in agen]

        assert asyncio.run(_collect()) == [0, 1, 0, 1, 2]

    def test_no_arg_sync_generator_ignores_sentinel_column(self):
        def items():
            yield "a"
            yield "b"

        _install_user_module(items=_NoOpFunction(items))
        ns = _load_shim(_generate_handler_shim("items"))
        event = {_NO_ARGS_SENTINEL_COLUMN: pa.array([True])}

        assert list(ns["handler"](event, {})) == ["a", "b"]

    def test_no_arg_call_serializes_one_sentinel_row(self):
        def ping() -> str:
            return "pong"

        fn = RemoteFunction(ping)
        batches = decode_ipc_stream(fn._serialize_call_args())

        assert len(batches) == 1
        assert batches[0].to_pydict() == {_NO_ARGS_SENTINEL_COLUMN: [True]}

    def test_arrow_dunder_input_and_output(self):
        def offset(point: ArrowPoint) -> ArrowPoint:
            assert isinstance(point, ArrowPoint)
            return ArrowPoint(point.x + 10, point.y + 20)

        _install_user_module(offset=offset)
        ns = _load_shim(_generate_handler_shim("offset"))
        event = {
            "point": pa.array(
                [{"x": 1, "y": 2}],
                type=_ARROW_POINT_PA_TYPE,
            )
        }

        result = ns["handler"](event, {})

        assert len(result) == 1
        assert isinstance(result[0], pa.StructScalar)
        assert result[0].type.equals(_ARROW_POINT_PA_TYPE)
        assert result[0].as_py() == {"x": 11, "y": 22}


class TestGenerateHandlerShimBatched:
    def test_signature(self):
        src = _generate_handler_shim("fn", batched=True)
        assert "def handler(events, contexts):" in src
        assert "_flatten_events(events, contexts)" in src
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

    def test_arrow_dunder_values(self):
        def offset(point: ArrowPoint) -> ArrowPoint:
            return ArrowPoint(point.x + 1, point.y + 1)

        _install_user_module(offset=offset)
        ns = _load_shim(_generate_handler_shim("offset", batched=True))
        events = [
            {
                "point": pa.array(
                    [{"x": 1, "y": 2}, {"x": 3, "y": 4}],
                    type=_ARROW_POINT_PA_TYPE,
                )
            }
        ]

        result = ns["handler"](events, [{}])

        assert [[value.as_py() for value in row] for row in result] == [
            [{"x": 2, "y": 3}, {"x": 4, "y": 5}]
        ]


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
        src = _generate_class_handler_shim("Mul", "mul", True, None, False, None, False)
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

    def test_wrapped_sync_generator_method(self):
        def wrapper(fn):
            @functools.wraps(fn)
            def inner(self, n: int):
                return fn(self, n)

            return inner

        class G:
            @wrapper
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
        assert "for ci, event in enumerate(events):" in src
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

        ipc_bytes = _encode_event(
            {"x": pa.array([1, 2, 3]), "y": pa.array([10, 20, 30])}
        )
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

    def test_arrow_dunder_roundtrip(self):
        from chalk_remote_call.servicer import process_batches

        def offset(point: ArrowPoint) -> ArrowPoint:
            assert isinstance(point, ArrowPoint)
            return ArrowPoint(point.x + 10, point.y + 20)

        function = RemoteFunction(offset)
        _install_user_module(offset=offset)
        ns = _load_shim(_generate_handler_shim("offset"))

        request = function._serialize_call_args(ArrowPoint(1, 2))
        request_batches = decode_ipc_stream(request)
        assert request_batches[0].column("point").type.equals(_ARROW_POINT_PA_TYPE)

        payloads = process_batches(request, ns["handler"], None, {})

        response_batches = [
            batch for payload in payloads for batch in decode_ipc_stream(payload)
        ]
        assert response_batches[0].column("result").type.equals(_ARROW_POINT_PA_TYPE)
        assert function._decode_result_chunks(payloads) == [ArrowPoint(11, 22)]

    def test_evaluation_scorer_result_list_roundtrip(self):
        from chalk_remote_call.servicer import process_batches

        def score(value: int) -> list[EvaluationScorerResult]:
            if value == 0:
                return []
            return [
                EvaluationScorerResult(
                    score=1.0 if value > 0 else 0.0,
                    metadata={"input": value},
                ),
                EvaluationScorerResult(
                    score=1.0 if value % 2 == 0 else 0.0,
                ),
            ]

        function = RemoteFunction(score)
        _install_user_module(score=score)
        ns = _load_shim(_generate_handler_shim("score"))

        payloads = process_batches(
            function._serialize_call_args(2), ns["handler"], None, {}
        )

        response_batches = [
            batch for payload in payloads for batch in decode_ipc_stream(payload)
        ]
        assert (
            response_batches[0]
            .column("result")
            .type.value_type.equals(EvaluationScorerResult.__chalk_pyarrow_type__())
        )
        assert function._decode_result_chunks(payloads) == [
            [
                EvaluationScorerResult(score=1.0, metadata={"input": 2}),
                EvaluationScorerResult(score=1.0),
            ]
        ]

        empty_payloads = process_batches(
            function._serialize_call_args(0), ns["handler"], None, {}
        )
        assert function._decode_result_chunks(empty_payloads) == [[]]

    def test_arrow_adapters_are_resolved_once(self, monkeypatch):
        hint_calls = 0
        bind_calls = 0
        get_type_hints = typing.get_type_hints
        signature_bind = inspect.Signature.bind

        def counting_get_type_hints(*args, **kwargs):
            nonlocal hint_calls
            hint_calls += 1
            return get_type_hints(*args, **kwargs)

        def counting_signature_bind(*args, **kwargs):
            nonlocal bind_calls
            bind_calls += 1
            return signature_bind(*args, **kwargs)

        monkeypatch.setattr(typing, "get_type_hints", counting_get_type_hints)
        monkeypatch.setattr(inspect.Signature, "bind", counting_signature_bind)

        def offset(point: ArrowPoint) -> ArrowPoint:
            return point

        function = RemoteFunction(offset)
        function._serialize_call_args(ArrowPoint(1, 2))
        function._serialize_call_args(ArrowPoint(3, 4))

        assert hint_calls == 1
        assert bind_calls == 0

    def test_compiled_argument_binder_applies_defaults_and_keywords(self):
        def configured(x: int, y: int = 2, *, z: int = 3) -> int:
            return x + y + z

        function = RemoteFunction(configured)
        batches = decode_ipc_stream(function._serialize_call_args(1, z=4))

        assert batches[0].to_pydict() == {"x": [1], "y": [2], "z": [4]}

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

    def test_async_generator_streams_per_yield(self):
        from chalk_remote_call.servicer import process_batches

        async def expand(n: int):
            for i in range(n):
                yield i

        _install_user_module(expand=expand)
        ns = _load_shim(_generate_handler_shim("expand"))

        ipc_bytes = _encode_event({"n": pa.array([2, 3])})
        payloads = process_batches(ipc_bytes, ns["handler"], None, {})
        assert _decode_result_column(payloads) == [[0], [1], [0], [1], [2]]

    def test_no_arg_sync_generator_streams_from_sentinel_call(self):
        from chalk_remote_call.servicer import process_batches

        def items():
            yield "a"
            yield "b"

        fn = RemoteFunction(items)
        _install_user_module(items=_NoOpFunction(items))
        ns = _load_shim(_generate_handler_shim("items"))

        payloads = process_batches(fn._serialize_call_args(), ns["handler"], None, {})
        assert _decode_result_column(payloads) == [["a"], ["b"]]

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


class TestRubricScorer:
    """The unmodified shim encodes rubric criteria, resolved from the annotation alone."""

    def _rubric(self):
        return Rubric(
            Criterion("directness", scale=(0, 2), description="Shortest path?"),
            Criterion("task_correctness", scale=(0, 2)),
        )

    def _judge_module(self, rubric, scores):
        from chalkcompute._function import _bind_rubric

        def judge(output: str) -> EvaluationScorerResult:
            return EvaluationScorerResult(score=0.5, criteria=scores)

        _bind_rubric(judge, rubric, label="judge")
        _install_user_module(judge=judge)

    def test_handler_encodes_criteria(self):
        rubric = self._rubric()
        self._judge_module(rubric, {"directness": 2, "task_correctness": 1})
        ns = _load_shim(_generate_handler_shim("judge"))

        results = ns["handler"]({"output": pa.array(["done"])}, None)

        assert len(results) == 1
        assert results[0].as_py() == {
            "score": 0.5,
            "metadata": None,
            "criteria": {"directness": 2.0, "task_correctness": 1.0},
        }
        assert results[0].type.equals(rubric.pyarrow_type())

    def test_handler_fails_the_row_when_a_criterion_is_missing(self):
        self._judge_module(self._rubric(), {"directness": 2})
        ns = _load_shim(_generate_handler_shim("judge"))

        with pytest.raises(ValueError, match="task_correctness"):
            ns["handler"]({"output": pa.array(["done"])}, None)
