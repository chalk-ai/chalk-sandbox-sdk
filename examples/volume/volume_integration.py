"""Increment a counter file on a shared volume from every surface that can
read and write volumes: the Volume API directly, a Sandbox, a Container,
and a RemoteFunction.

Each surface reads /data/counter.txt, increments the integer, and writes it
back. The file is created on first run.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chalkcompute import (
    Container,
    Image,
    RemoteFunction,
    Sandbox,
    Volume,
    VolumeNotFoundError,
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
    except VolumeNotFoundError:
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
    except VolumeNotFoundError:
        n = 0
    vol.put_file(COUNTER_PATH, str(n + 1))
    print(f"volume:    {n} -> {n + 1}")

    # Attach to Container
    container = Container(
        image=Image.debian_slim(python_version="3.12"),
        volumes=[(COUNTER_VOLUME, MOUNT)],
    ).run()
    try:
        result = container.exec("sh", "-c", BUMP_SH)
        print(f"container: -> {result.stdout_text.strip()}")
    finally:
        container.stop()

    # Attach to a RemoteFunction
    bump_via_function = RemoteFunction(
        _bump_via_function,
        image=Image.debian_slim(python_version="3.12"),
        cpu="1",
        memory="1Gi",
        volumes=[(COUNTER_VOLUME, MOUNT)],
    )
    bump_via_function.deploy()
    print(f"function:  -> {bump_via_function.remote()}")

    # Attach to a Sandbox
    sandbox = Sandbox(
        cpu="1",
        memory="1Gi",
        image=Image.debian_slim(python_version="3.12"),
        volumes=[(COUNTER_VOLUME, MOUNT)],
    ).run()
    try:
        result = sandbox.exec("sh", "-c", BUMP_SH)
        print(f"sandbox:   -> {result.stdout_text.strip()}")
    finally:
        sandbox.terminate()

    print(f"end:   counter = {read_counter()}")
