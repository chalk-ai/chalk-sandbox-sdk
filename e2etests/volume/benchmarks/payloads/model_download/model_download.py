#!/usr/bin/env python3
"""Reconstruct scaled model shards inside a sandbox."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import random
import struct
import time


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mount-path", required=True)
    parser.add_argument("--mode", choices=("direct", "sequential"), required=True)
    parser.add_argument("--large-shard-mib", type=positive_int, required=True)
    parser.add_argument("--small-shard-mib", type=positive_int, required=True)
    parser.add_argument("--extent-mib", type=positive_int, required=True)
    return parser.parse_args()


def write_shard(path: Path, size: int, extent_size: int, mode: str, seed: int) -> int:
    offsets = list(range(0, size, extent_size))
    if mode == "direct":
        random.Random(seed).shuffle(offsets)

    payload = bytearray(extent_size)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
    try:
        for offset in offsets:
            length = min(extent_size, size - offset)
            struct.pack_into("<QQ", payload, 0, seed, offset)
            remaining = memoryview(payload)[:length]
            position = offset
            while remaining:
                written = os.pwrite(fd, remaining, position)
                if written == 0:
                    raise RuntimeError(
                        f"Write made no progress for {path} at {position}"
                    )
                position += written
                remaining = remaining[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return size


def main() -> None:
    args = parse_args()
    mib = 1024**2
    extent_size = args.extent_mib * mib
    shard_sizes = (args.large_shard_mib * mib, args.small_shard_mib * mib)
    mount_path = Path(args.mount_path)
    mount_path.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                write_shard,
                mount_path / f"model-{index + 1:05d}-of-00002.safetensors",
                size,
                extent_size,
                args.mode,
                index + 1,
            )
            for index, size in enumerate(shard_sizes)
        ]
        total_bytes = sum(future.result() for future in futures)
    elapsed_seconds = time.monotonic() - started

    result = {
        "mode": args.mode,
        "large_shard_bytes": shard_sizes[0],
        "small_shard_bytes": shard_sizes[1],
        "extent_bytes": extent_size,
        "total_bytes": total_bytes,
        "elapsed_seconds": elapsed_seconds,
        "bw_bytes_per_sec": total_bytes / elapsed_seconds,
        "jobs": 2,
    }
    (mount_path / "reconstruction-result.json").write_text(
        json.dumps(result, sort_keys=True)
    )
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
