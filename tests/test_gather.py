import asyncio
import inspect
import time
from typing import Any

import chalkcompute


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
    import pytest

    with pytest.raises(TypeError, match="use .defer"):
        chalkcompute.gather([_AsyncHandle("ok"), [1.0, 2.0, 3.0]])
