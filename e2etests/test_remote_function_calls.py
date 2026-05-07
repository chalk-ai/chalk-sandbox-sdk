"""End-to-end tests for invoking deployed functions through chalkcompute only.

These tests intentionally avoid chalkdf. They attach to functions that are
already deployed in the target environment and exercise the direct scalar path,
the queue-backed defer/get path, and streamed generator results.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from collections.abc import AsyncIterator, Iterable
from typing import Any

import pytest
from dotenv import load_dotenv

import chalkcompute

load_dotenv()


SYNC_SCALAR_FUNCTION = "chalk-sandbox-test-add"
ASYNC_SCALAR_FUNCTION = "chalk-sandbox-test-async-add"
SYNC_GENERATOR_FUNCTION = "chalk-sandbox-test-count-up"
ASYNC_GENERATOR_FUNCTION = "chalk-sandbox-test-async-count-up"

needs_chalk_auth = pytest.mark.skipif(
    not os.environ.get("CHALK_API_SERVER") or not os.environ.get("CHALK_CLIENT_ID"),
    reason="requires CHALK_API_SERVER and CHALK_CLIENT_ID",
)


@needs_chalk_auth
def test_native_remote_call_extension_available() -> None:
    assert "chalkdf" not in sys.modules

    native = importlib.import_module("chalkcompute._remote_call_native")
    assert hasattr(native, "RemoteCallClient")


def _result_chunks(batches: Iterable[Any]) -> list[list[Any]]:
    return [batch.column("result").to_pylist() for batch in batches]


async def _async_result_chunks(batches: AsyncIterator[Any]) -> list[list[Any]]:
    chunks = []
    async for batch in batches:
        chunks.append(batch.column("result").to_pylist())
    return chunks


@needs_chalk_auth
def test_sync_scalar_direct_and_defer() -> None:
    assert "chalkdf" not in sys.modules

    fn = chalkcompute.RemoteFunction.from_name(SYNC_SCALAR_FUNCTION)

    assert fn(1, 10) == 11

    handle = fn.defer(2, 20)
    assert handle.get(timeout=60) == 22


@needs_chalk_auth
def test_async_scalar_direct_and_defer() -> None:
    assert "chalkdf" not in sys.modules

    fn = chalkcompute.RemoteFunction.from_name(ASYNC_SCALAR_FUNCTION)

    assert asyncio.run(fn(3, 30)) == 33

    handle = fn.defer(4, 40)
    assert asyncio.run(handle.get(timeout=60)) == 44


@needs_chalk_auth
def test_sync_generator_direct_and_defer() -> None:
    assert "chalkdf" not in sys.modules

    fn = chalkcompute.RemoteFunction.from_name(SYNC_GENERATOR_FUNCTION)

    assert _result_chunks(fn(5)) == [[0], [1], [2], [3], [4]]

    stream_handle = fn.defer(3)
    assert _result_chunks(stream_handle.stream()) == [[0], [1], [2]]

    get_handle = fn.defer(3)
    assert get_handle.get(timeout=60) == [0, 1, 2]


@needs_chalk_auth
def test_async_generator_direct_and_defer() -> None:
    assert "chalkdf" not in sys.modules

    fn = chalkcompute.RemoteFunction.from_name(ASYNC_GENERATOR_FUNCTION)

    assert asyncio.run(_async_result_chunks(fn(5))) == [
        [0],
        [1],
        [2],
        [3],
        [4],
    ]

    stream_handle = fn.defer(3)
    assert asyncio.run(_async_result_chunks(stream_handle.stream())) == [
        [0],
        [1],
        [2],
    ]

    get_handle = fn.defer(3)
    assert asyncio.run(get_handle.get(timeout=60)) == [0, 1, 2]
