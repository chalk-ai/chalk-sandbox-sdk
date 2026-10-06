import asyncio
import inspect
import threading
import time
from typing import Any
from unittest.mock import Mock

import chalkcompute
import pyarrow as pa
import pytest
from chalk.client._model_remote import ModelCallHandle, ModelRemoteError
from chalkcompute._function import FunctionCallHandle


class _AsyncHandle:
    def __init__(self, value: Any, *, delay: float = 0) -> None:
        self.value = value
        self.delay = delay
        self.timeouts: list[float | None] = []

    def get(self, timeout: float | None = None) -> Any:
        self.timeouts.append(timeout)

        async def _get() -> Any:
            await asyncio.sleep(self.delay)
            return self.value

        return _get()


def test_gather_returns_results_in_input_order() -> None:
    handles = [_AsyncHandle(1), _AsyncHandle(2), _AsyncHandle(3)]

    assert chalkcompute.gather(handles, timeout=12.5) == [1, 2, 3]
    assert [handle.timeouts for handle in handles] == [[12.5], [12.5], [12.5]]


def test_gather_waits_concurrently_in_sync_context() -> None:
    handles = [
        _AsyncHandle("a", delay=0.1),
        _AsyncHandle("b", delay=0.1),
        _AsyncHandle("c", delay=0.1),
    ]

    started_at = time.monotonic()
    assert chalkcompute.gather(handles) == ["a", "b", "c"]
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.25


def test_gather_returns_awaitable_in_async_context() -> None:
    async def run() -> list[str]:
        result = chalkcompute.gather([_AsyncHandle("a"), _AsyncHandle("b")])
        assert inspect.isawaitable(result)
        return await result

    assert asyncio.run(run()) == ["a", "b"]


def test_gather_empty_iterable() -> None:
    assert chalkcompute.gather([]) == []


def test_gather_rejects_non_handles_with_helpful_message() -> None:
    # A common mistake: gather(fn.remote(x) ...) passes results, not handles.
    with pytest.raises(TypeError, match="use .defer"):
        chalkcompute.gather([_AsyncHandle("ok"), [1.0, 2.0, 3.0]])


@pytest.mark.parametrize("async_context", [False, True])
def test_gather_model_and_function_handles_concurrently(async_context: bool) -> None:
    barrier = threading.Barrier(3, timeout=5)

    def poll(*args: Any, **kwargs: Any) -> Mock:
        # All three polls must start before any returns, including in async code.
        barrier.wait()
        sink = pa.BufferOutputStream()
        batch = pa.record_batch([[7.5]], names=["prediction"])
        with pa.ipc.new_stream(sink, batch.schema) as writer:
            writer.write_batch(batch)
        return Mock(
            cursor="done",
            poll_count=1,
            status_name="completed",
            chunks=[sink.getvalue().to_pybytes()],
            is_completed=True,
            is_failed=False,
            is_expired=False,
        )

    client = Mock()
    client.poll_next.side_effect = poll
    models = [ModelCallHandle(lambda: client, "model", f"call-{i}") for i in range(2)]
    function = Mock(_func_name="function", _is_async=False)
    function.poll.side_effect = poll
    function._materialize_result_chunks.return_value = "function result"
    handles = [models[0], FunctionCallHandle(function, "fn-call"), models[1]]

    async def run() -> list[Any]:
        return await chalkcompute.gather(handles, timeout=10)

    results = (
        asyncio.run(run())
        if async_context
        else chalkcompute.gather(handles, timeout=10)
    )
    assert results == [7.5, "function result", 7.5]


def test_gather_propagates_model_error() -> None:
    client = Mock()
    client.poll_next.return_value = Mock(
        cursor="done",
        poll_count=1,
        status_name="failed",
        chunks=[],
        is_failed=True,
        error_message="inference failed",
    )
    handle = ModelCallHandle(lambda: client, "model", "call")
    with pytest.raises(ModelRemoteError, match="inference failed"):
        chalkcompute.gather([handle])
