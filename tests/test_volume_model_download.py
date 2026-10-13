from __future__ import annotations

import errno
import os
from pathlib import Path
import random
import struct

import pytest

from e2etests.volume.benchmarks.payloads.model_download import model_download


@pytest.mark.parametrize("mode", ["direct", "sequential"])
def test_shard_retries_partial_writes(tmp_path: Path, monkeypatch, mode: str) -> None:
    path = tmp_path / "model.safetensors"
    extent_size = 4 * 1024**2
    size = 2 * extent_size + 1024**2 + 19
    pwrite = os.pwrite
    calls: list[tuple[int, int]] = []

    def partial_write(fd: int, data: memoryview, offset: int) -> int:
        calls.append((offset, len(data)))
        return pwrite(fd, data[: 1024**2], offset)

    monkeypatch.setattr(model_download.os, "pwrite", partial_write)

    assert model_download.write_shard(path, size, extent_size, mode, 7) == size

    offsets = list(range(0, size, extent_size))
    if mode == "direct":
        random.Random(7).shuffle(offsets)
    expected_calls = [
        (position, min(extent_size, size - offset) - (position - offset))
        for offset in offsets
        for position in range(offset, min(offset + extent_size, size), 1024**2)
    ]
    assert calls == expected_calls
    expected = bytearray(size)
    for offset in offsets:
        struct.pack_into("<QQ", expected, offset, 7, offset)
    assert path.read_bytes() == expected


def test_shard_rejects_zero_progress(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(model_download.os, "pwrite", lambda *_args: 0)
    with pytest.raises(RuntimeError, match="Write made no progress"):
        model_download.write_shard(tmp_path / "model", 64, 32, "sequential", 1)


def test_shard_propagates_error_after_partial_write(
    tmp_path: Path, monkeypatch
) -> None:
    pwrite = os.pwrite

    def failing_write(fd: int, data: memoryview, offset: int) -> int:
        if offset == 0:
            return pwrite(fd, data[:16], offset)
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(model_download.os, "pwrite", failing_write)
    with pytest.raises(OSError) as error:
        model_download.write_shard(tmp_path / "model", 64, 32, "sequential", 1)
    assert error.value.errno == errno.ENOSPC
