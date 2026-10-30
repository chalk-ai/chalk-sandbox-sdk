#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute>=2.11.1"]
# ///

"""Increment a counter file on a shared volume from every surface that can
read and write volumes: the Volume API directly, a Sandbox, and a
RemoteFunction.

Each surface reads /data/counter.txt, increments the integer, and writes it
back. The file is created on first run. The named volume intentionally persists
so subsequent runs continue the same counter; the function deployment and
sandbox created by each run are deleted.
"""

import uuid

from chalkcompute import (
    Image,
    RemoteFunction,
    Sandbox,
    Volume,
    VolumeFileNotFoundError,
)

COUNTER_VOLUME = "volume_counters"
COUNTER_PATH = "volume_counter.txt"
MOUNT = "/data"

BUMP_SH = (
    f"p={MOUNT}/{COUNTER_PATH}; "
    'n=$(cat "$p" 2>/dev/null || echo 0); '
    'echo $((n + 1)) > "$p"; '
    'cat "$p"'
)


def read_counter() -> int:
    try:
        return int(Volume(COUNTER_VOLUME).read_file(COUNTER_PATH).decode().strip())
    except VolumeFileNotFoundError:
        return 0


def _bump_via_function() -> int:
    from pathlib import Path

    p = Path(MOUNT) / COUNTER_PATH
    n = int(p.read_text().strip()) if p.exists() else 0
    p.write_text(str(n + 1))
    return n + 1


if __name__ == "__main__":
    # Regular Volume API
    vol = Volume(COUNTER_VOLUME)
    try:
        n = int(vol.read_file(COUNTER_PATH).decode().strip())
    except VolumeFileNotFoundError:
        n = 0
    vol.put_file(COUNTER_PATH, str(n + 1))
    print(f"volume:    {n} -> {n + 1}")

    # Attach to a RemoteFunction
    bump_via_function = RemoteFunction(
        _bump_via_function,
        name=f"volume-counter-bump-{uuid.uuid4().hex[:8]}",
        image=Image.debian_slim(python_version="3.12"),
        volumes=[(COUNTER_VOLUME, MOUNT)],
    )
    try:
        bump_via_function.deploy()
        print(f"function:  -> {bump_via_function.remote()}")
    finally:
        bump_via_function.delete()

    # Attach to a Sandbox
    sandbox = Sandbox(
        image=Image.debian_slim(python_version="3.12"),
        volumes=[(COUNTER_VOLUME, MOUNT)],
    )
    try:
        sandbox.run()
        result = sandbox.exec("sh", "-c", BUMP_SH)
        print(f"sandbox:   -> {result.stdout_text.strip()}")
    finally:
        if sandbox.id is not None:
            sandbox.terminate()

    print(f"end:   counter = {read_counter()}")
