from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterable
from typing import Any, Protocol

from chalkcompute._async_utils import _has_running_event_loop, _run_in_worker_thread


class _DeferredCallHandle(Protocol):
    def get(self, timeout: float | None = None) -> Any: ...


async def _gather_get(handle: _DeferredCallHandle, timeout: float | None) -> Any:
    # Model handles block in get(); invoke them off-loop so every call can progress.
    result = await _run_in_worker_thread(handle.get, timeout=timeout)
    if inspect.isawaitable(result):
        return await result
    return result


async def _gather_async(
    handles: list[_DeferredCallHandle],
    *,
    timeout: float | None = None,
) -> list[Any]:
    return list(
        await asyncio.gather(*(_gather_get(handle, timeout) for handle in handles))
    )


def gather_handles(
    handles: Iterable[_DeferredCallHandle],
    *,
    timeout: float | None = None,
) -> Any:
    """Collect deferred-call handles concurrently, preserving input order.

    This is the shared SDK interface used by chalkcompute's function gather and
    chalkpy's model gather. Each handle must implement ``get(timeout=...)``.
    Returns a list in synchronous code or an awaitable in a running event loop.
    """
    handle_list = list(handles)
    for i, handle in enumerate(handle_list):
        if not callable(getattr(handle, "get", None)):
            raise TypeError(
                f"gather() expects deferred-call handles from .defer(), but item "
                f"{i} is a {type(handle).__name__}. .remote() runs the call and "
                f"returns its result, not a handle — use .defer() to fan out, e.g. "
                f"gather(fn.defer(x) for x in xs)."
            )
    if _has_running_event_loop():
        return _gather_async(handle_list, timeout=timeout)
    return asyncio.run(_gather_async(handle_list, timeout=timeout))
