"""Regression test: chalkcompute protos must coexist with chalkpy in one process.

chalkcompute's generated `_pb2.py` modules used to register file descriptors
into protobuf's process-wide Default pool. Importing chalkcompute alongside
chalkpy (which registers the same file names — `chalk/auth/v1/permissions.proto`,
etc.) then raised `TypeError: Couldn't build proto file into descriptor pool:
duplicate file name ...`. The fix routes chalkcompute through a private
`DescriptorPool` (`chalkcompute._proto_pool.DESCRIPTOR_POOL`).

This test spins up a fresh `uv run --with chalkpy==X.Y.Z` subprocess for each
pinned chalkpy version and imports the overlapping proto modules from both
libraries — in both orders — asserting no exception.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

CHALKPY_VERSIONS = [
    "2.121.0",
    "2.124.2",
    "2.128.3",
]

OVERLAPPING_MODULES = [
    "chalk._gen.chalk.auth.v1.permissions_pb2",
    "chalk._gen.chalk.utils.v1.encoding_pb2",
    "chalk._gen.chalk.arrow.v1.arrow_pb2",
]
CHALKCOMPUTE_MIRRORS = [
    "chalkcompute._gen.chalk.auth.v1.permissions_pb2",
    "chalkcompute._gen.chalk.utils.v1.encoding_pb2",
    "chalkcompute._gen.chalk.arrow.v1.arrow_pb2",
]


def _import_script(chalkpy_first: bool) -> str:
    chalkpy_imports = "\n".join(f"import {m}" for m in OVERLAPPING_MODULES)
    chalkcompute_imports = "\n".join(f"import {m}" for m in CHALKCOMPUTE_MIRRORS)
    first, second = (
        (chalkpy_imports, chalkcompute_imports)
        if chalkpy_first
        else (chalkcompute_imports, chalkpy_imports)
    )
    # Importing both sets of generated _pb2 modules in one process is what
    # used to raise `TypeError: Couldn't build proto file into descriptor pool:
    # duplicate file name chalk/auth/v1/permissions.proto`. If the imports
    # succeed and we print "OK", the descriptor pools are isolated.
    return f"{first}\n{second}\nprint('OK')\n"


@pytest.fixture(scope="module")
def uv_bin() -> str:
    path = shutil.which("uv")
    if not path:
        pytest.skip("uv not on PATH; required to install chalkpy in isolation")
    return path


@pytest.mark.parametrize("chalkpy_version", CHALKPY_VERSIONS)
@pytest.mark.parametrize("chalkpy_first", [True, False], ids=["chalkpy_first", "chalkcompute_first"])
def test_chalkpy_chalkcompute_coexist(
    uv_bin: str, chalkpy_version: str, chalkpy_first: bool
) -> None:
    script = _import_script(chalkpy_first=chalkpy_first)
    result = subprocess.run(
        [uv_bin, "run", "--with", f"chalkpy=={chalkpy_version}", "python", "-c", script],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        f"chalkpy=={chalkpy_version} (chalkpy_first={chalkpy_first}) failed:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "OK" in result.stdout, f"unexpected stdout: {result.stdout!r}"
