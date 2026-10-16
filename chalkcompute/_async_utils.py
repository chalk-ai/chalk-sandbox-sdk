from __future__ import annotations

import asyncio
import contextvars
import threading
from typing import Any, Callable


def _has_running_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


async def _run_in_worker_thread(
    func: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()
    context = contextvars.copy_context()

    def complete_with_result(value: Any) -> None:
        if not future.cancelled():
            future.set_result(value)

    def complete_with_exception(exc: BaseException) -> None:
        if not future.cancelled():
            future.set_exception(exc)

    def run() -> None:
        try:
            value = context.run(func, *args, **kwargs)
        except BaseException as exc:
            loop.call_soon_threadsafe(complete_with_exception, exc)
        else:
            loop.call_soon_threadsafe(complete_with_result, value)

    threading.Thread(target=run, daemon=True).start()
    return await future
