"""Unit tests for transitive local-dependency collection."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from chalkcompute._deps import (
    collect_local_sources,
    find_sys_path_root,
    module_name_for,
)


def _mk(root: Path, rel: str, body: str = "") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body))
    return p


def _modules(result) -> list[str]:
    return sorted(s.module_name for s in result.sources)


def _mounts(result) -> list[str]:
    return sorted(s.mount_path for s in result.sources)


def test_loose_script_no_imports(tmp_path: Path) -> None:
    entry = _mk(tmp_path, "script.py", "def f(x: int) -> int: return x")
    result = collect_local_sources(entry)
    assert result.sys_path_root == tmp_path.resolve()
    assert _modules(result) == ["script"]
    assert _mounts(result) == ["/app/script.py"]


def test_package_with_absolute_imports(tmp_path: Path) -> None:
    _mk(tmp_path, "mypkg/__init__.py", "")
    _mk(
        tmp_path,
        "mypkg/utils.py",
        """
        def helper(x: int) -> int:
            return x + 1
        """,
    )
    entry = _mk(
        tmp_path,
        "mypkg/main.py",
        """
        from mypkg.utils import helper

        def f(x: int) -> int:
            return helper(x)
        """,
    )
    result = collect_local_sources(entry)
    assert result.sys_path_root == tmp_path.resolve()
    assert _modules(result) == ["mypkg", "mypkg.main", "mypkg.utils"]
    assert _mounts(result) == [
        "/app/mypkg/__init__.py",
        "/app/mypkg/main.py",
        "/app/mypkg/utils.py",
    ]


def test_relative_imports(tmp_path: Path) -> None:
    _mk(tmp_path, "pkg/__init__.py", "")
    _mk(tmp_path, "pkg/sub/__init__.py", "")
    _mk(tmp_path, "pkg/sub/helper.py", "VALUE = 1")
    _mk(tmp_path, "pkg/peer.py", "NAME = 'peer'")
    entry = _mk(
        tmp_path,
        "pkg/sub/entry.py",
        """
        from . import helper
        from .. import peer

        def f(x: int) -> int:
            return x + helper.VALUE
        """,
    )
    result = collect_local_sources(entry)
    assert _modules(result) == [
        "pkg",
        "pkg.peer",
        "pkg.sub",
        "pkg.sub.entry",
        "pkg.sub.helper",
    ]


def test_pip_package_imports_are_skipped(tmp_path: Path) -> None:
    entry = _mk(
        tmp_path,
        "script.py",
        """
        import pyarrow as pa
        import os
        import sys

        def f(x: int) -> int:
            return x
        """,
    )
    result = collect_local_sources(entry)
    assert _modules(result) == ["script"]


def test_from_x_import_y_resolves_submodule(tmp_path: Path) -> None:
    _mk(tmp_path, "pkg/__init__.py", "")
    _mk(tmp_path, "pkg/child.py", "VALUE = 2")
    entry = _mk(
        tmp_path,
        "pkg/entry.py",
        """
        from pkg import child

        def f(x: int) -> int:
            return x + child.VALUE
        """,
    )
    result = collect_local_sources(entry)
    assert _modules(result) == ["pkg", "pkg.child", "pkg.entry"]


def test_find_sys_path_root_package(tmp_path: Path) -> None:
    _mk(tmp_path, "a/__init__.py", "")
    _mk(tmp_path, "a/b/__init__.py", "")
    f = _mk(tmp_path, "a/b/mod.py", "")
    assert find_sys_path_root(f) == tmp_path.resolve()


def test_find_sys_path_root_loose_file(tmp_path: Path) -> None:
    f = _mk(tmp_path, "script.py", "")
    assert find_sys_path_root(f) == tmp_path.resolve()


def test_module_name_for(tmp_path: Path) -> None:
    _mk(tmp_path, "a/__init__.py", "")
    assert module_name_for(tmp_path / "a" / "__init__.py", tmp_path) == "a"
    _mk(tmp_path, "a/b.py", "")
    assert module_name_for(tmp_path / "a" / "b.py", tmp_path) == "a.b"


def test_skips_self_reference(tmp_path: Path) -> None:
    entry = _mk(
        tmp_path,
        "cycle.py",
        """
        import cycle

        def f(x: int) -> int:
            return x
        """,
    )
    result = collect_local_sources(entry)
    assert _modules(result) == ["cycle"]


def test_diamond_dependency(tmp_path: Path) -> None:
    _mk(tmp_path, "pkg/__init__.py", "")
    _mk(tmp_path, "pkg/shared.py", "VALUE = 0")
    _mk(tmp_path, "pkg/left.py", "from pkg import shared")
    _mk(tmp_path, "pkg/right.py", "from pkg import shared")
    entry = _mk(
        tmp_path,
        "pkg/entry.py",
        """
        from pkg import left, right

        def f(x: int) -> int:
            return x
        """,
    )
    result = collect_local_sources(entry)
    # shared only appears once despite two import paths
    assert _modules(result) == [
        "pkg",
        "pkg.entry",
        "pkg.left",
        "pkg.right",
        "pkg.shared",
    ]


@pytest.mark.parametrize(
    "body",
    [
        "def f(x: int\n",  # unterminated signature
        "this is :: not python",
    ],
)
def test_malformed_dependency_is_skipped(tmp_path: Path, body: str) -> None:
    _mk(tmp_path, "pkg/__init__.py", "")
    _mk(tmp_path, "pkg/bad.py", body)
    entry = _mk(
        tmp_path,
        "pkg/entry.py",
        """
        from pkg import bad  # noqa

        def f(x: int) -> int:
            return x
        """,
    )
    result = collect_local_sources(entry)
    # bad.py is still shipped (AST parse failure is silent), entry is shipped
    assert "pkg.entry" in _modules(result)
    assert "pkg.bad" in _modules(result)
