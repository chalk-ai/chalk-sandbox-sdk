"""Tests for helper-function support: package auto-include and
``Image.add_local_python_source``."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from chalkcompute._function import (
    RemoteFunction,
    _generate_handler_shim,
    _package_dir_for_module,
    _prepare_package_dir,
)
from chalkcompute._image import Image


def _pkg_add(x: int, y: int) -> int:
    return x + y


def test_package_dir_for_module() -> None:
    tests_dir = str(Path(__file__).resolve().parent)
    assert _package_dir_for_module("tests.test_helper_sources") == tests_dir
    assert _package_dir_for_module("test_helper_sources") is None
    assert _package_dir_for_module("nonexistent_pkg_xyz.mod") is None


def test_handler_shim_module_name() -> None:
    assert "from pkg.mod import fn" in _generate_handler_shim(
        "fn", module_name="pkg.mod"
    )
    assert "from pkg.mod import fn" in _generate_handler_shim(
        "fn", module_name="pkg.mod", batched=True
    )
    assert "from user_module import fn" in _generate_handler_shim("fn")


def test_package_function_ships_package_and_imports_real_module() -> None:
    with patch.dict(os.environ, {"CHALK_FUNCTION_STRIP_SOURCE": "0"}, clear=False):
        fn = RemoteFunction(_pkg_add)
        image = fn._build_deploy_image()

    dests = {f.dest for f in image._lazy_local_files}
    assert "/app/user_module.py" not in dests
    assert "/app/tests/test_helper_sources.py" in dests
    assert not any("__pycache__" in d for d in dests)

    shim_src = next(
        f.src for f in image._lazy_local_files if f.dest == "/app/handler.py"
    )
    shim = Path(shim_src).read_text()
    assert "from tests.test_helper_sources import _pkg_add" in shim


def test_add_local_python_source_single_module(tmp_path, monkeypatch) -> None:
    (tmp_path / "my_helper_mod.py").write_text("def f():\n    return 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    image = Image.debian_slim().add_local_python_source("my_helper_mod")

    dests = {f.dest for f in image._lazy_local_files}
    assert "/app/my_helper_mod.py" in dests


def test_add_local_python_source_package(tmp_path, monkeypatch) -> None:
    pkg = tmp_path / "my_helper_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "util.py").write_text("def f():\n    return 1\n")
    pycache = pkg / "__pycache__"
    pycache.mkdir()
    (pycache / "util.cpython-311.pyc").write_bytes(b"junk")
    monkeypatch.syspath_prepend(str(tmp_path))

    image = Image.debian_slim().add_local_python_source("my_helper_pkg")

    dests = {f.dest for f in image._lazy_local_files}
    assert "/app/my_helper_pkg/__init__.py" in dests
    assert "/app/my_helper_pkg/util.py" in dests
    assert not any("__pycache__" in d for d in dests)


def test_add_local_python_source_rejects_dotted_and_missing() -> None:
    with pytest.raises(ValueError, match="top-level"):
        Image.debian_slim().add_local_python_source("a.b")
    with pytest.raises(ValueError, match="not found"):
        Image.debian_slim().add_local_python_source("no_such_module_xyz_123")


def test_single_file_entrypoint_renamed_on_collision(tmp_path, monkeypatch) -> None:
    (tmp_path / "user_module.py").write_text("def f():\n    return 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    def add(x: int, y: int) -> int:
        return x + y

    add.__module__ = "__main__"

    with patch.dict(os.environ, {"CHALK_FUNCTION_STRIP_SOURCE": "0"}, clear=False):
        fn = RemoteFunction(
            add, image=Image.debian_slim().add_local_python_source("user_module")
        )
        image = fn._build_deploy_image()

    dests = {f.dest for f in image._lazy_local_files}
    assert "/app/user_module.py" in dests
    assert "/app/user_module_2.py" in dests
    shim_src = next(
        f.src for f in image._lazy_local_files if f.dest == "/app/handler.py"
    )
    assert "from user_module_2 import add" in Path(shim_src).read_text()


def test_package_named_handler_renames_shim(tmp_path, monkeypatch) -> None:
    import sys
    import types

    pkg_dir = tmp_path / "handler"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "fns.py").write_text(
        "def add(x: int, y: int) -> int:\n    return x + y\n"
    )

    fake_pkg = types.ModuleType("handler")
    fake_pkg.__path__ = [str(pkg_dir)]
    monkeypatch.setitem(sys.modules, "handler", fake_pkg)

    def add(x: int, y: int) -> int:
        return x + y

    add.__module__ = "handler.fns"

    with patch.dict(os.environ, {"CHALK_FUNCTION_STRIP_SOURCE": "0"}, clear=False):
        fn = RemoteFunction(add)
        image = fn._build_deploy_image()

    dests = {f.dest for f in image._lazy_local_files}
    assert "/app/handler/fns.py" in dests
    assert "/app/handler_2.py" in dests
    assert fn._handler_module == "handler_2"
    shim_src = next(
        f.src for f in image._lazy_local_files if f.dest == "/app/handler_2.py"
    )
    assert "from handler.fns import add" in Path(shim_src).read_text()


def test_prepare_package_dir_no_strip_returns_original(tmp_path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")

    with patch.dict(os.environ, {"CHALK_FUNCTION_STRIP_SOURCE": "0"}, clear=False):
        prepared, is_temp = _prepare_package_dir(
            str(pkg),
            label="Function 'fn'",
            entrypoint_file=str(pkg / "__init__.py"),
            entrypoint_sources=[],
        )

    assert prepared == str(pkg)
    assert not is_temp


def test_prepare_package_dir_strips_chalkcompute(tmp_path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    entry = pkg / "fns.py"
    entry.write_text(
        "import chalkcompute\n"
        "\n"
        "@chalkcompute.function(name='x')\n"
        "def fn(a: int) -> int:\n"
        "    return a\n"
    )
    (pkg / "helper.py").write_text("import chalkcompute\n\ndef h():\n    return 1\n")

    with patch.dict(os.environ, {"CHALK_FUNCTION_STRIP_SOURCE": "1"}, clear=False):
        prepared, is_temp = _prepare_package_dir(
            str(pkg),
            label="Function 'fn'",
            entrypoint_file=str(entry),
            entrypoint_sources=["def fn(a: int) -> int:\n    return a\n"],
        )

    assert is_temp
    stripped_entry = (Path(prepared) / "fns.py").read_text()
    assert "chalkcompute" not in stripped_entry
    assert "def fn" in stripped_entry
    assert "chalkcompute" not in (Path(prepared) / "helper.py").read_text()


def _fake_main(spec_name: str | None, origin: str | None):
    """A stand-in ``__main__`` module with a controlled ``__spec__``."""
    import importlib.machinery
    import types

    module = types.ModuleType("__main__")
    module.__spec__ = (
        importlib.machinery.ModuleSpec(spec_name, None, origin=origin)
        if spec_name is not None
        else None
    )
    return module


def _main_like(func):
    """Clone ``func`` with ``__module__`` forced to ``__main__``."""
    clone = type(func)(
        func.__code__, func.__globals__, name=func.__name__, closure=func.__closure__
    )
    clone.__annotations__ = dict(func.__annotations__)
    clone.__module__ = "__main__"
    return clone


def test_deploy_module_name_recovers_dash_m(monkeypatch) -> None:
    import sys

    from chalkcompute._function import _deploy_module_name

    monkeypatch.setitem(
        sys.modules,
        "__main__",
        _fake_main("tests.test_helper_sources", str(Path(__file__).resolve())),
    )
    assert _deploy_module_name(_main_like(_pkg_add)) == "tests.test_helper_sources"

    # Imported functions keep their real module name even when __main__ has a
    # spec pointing elsewhere.
    monkeypatch.setitem(
        sys.modules,
        "__main__",
        _fake_main("other.module", str(Path(__file__).resolve())),
    )
    assert _deploy_module_name(_pkg_add) == "tests.test_helper_sources"


def test_deploy_module_name_keeps_main_for_scripts(monkeypatch) -> None:
    import sys

    from chalkcompute._function import _deploy_module_name

    fn = _main_like(_pkg_add)

    # Direct script run: no spec at all.
    monkeypatch.setitem(sys.modules, "__main__", _fake_main(None, None))
    assert _deploy_module_name(fn) == "__main__"

    # An unrelated -m runner (e.g. python -m pytest): spec origin is not the
    # function's file, so the spec name must not be adopted.
    monkeypatch.setitem(
        sys.modules, "__main__", _fake_main("pytest.__main__", "/nonexistent/xyz.py")
    )
    assert _deploy_module_name(fn) == "__main__"


def test_dash_m_run_ships_package(monkeypatch) -> None:
    import sys

    monkeypatch.setitem(
        sys.modules,
        "__main__",
        _fake_main("tests.test_helper_sources", str(Path(__file__).resolve())),
    )

    with patch.dict(os.environ, {"CHALK_FUNCTION_STRIP_SOURCE": "0"}, clear=False):
        fn = RemoteFunction(_main_like(_pkg_add))
        image = fn._build_deploy_image()

    dests = {f.dest for f in image._lazy_local_files}
    assert "/app/tests/test_helper_sources.py" in dests
    assert not any(d.endswith("user_module.py") for d in dests)
    shim_src = next(
        f.src for f in image._lazy_local_files if f.dest == "/app/handler.py"
    )
    assert (
        "from tests.test_helper_sources import _pkg_add" in Path(shim_src).read_text()
    )


def test_loose_imported_module_ships_single_file() -> None:
    fn_clone = _main_like(_pkg_add)
    fn_clone.__module__ = "loose_mod"

    with patch.dict(os.environ, {"CHALK_FUNCTION_STRIP_SOURCE": "0"}, clear=False):
        fn = RemoteFunction(fn_clone)
        image = fn._build_deploy_image()

    dests = {f.dest for f in image._lazy_local_files}
    assert "/app/user_module.py" in dests
    assert "/app/tests/test_helper_sources.py" not in dests
    shim_src = next(
        f.src for f in image._lazy_local_files if f.dest == "/app/handler.py"
    )
    assert "from user_module import _pkg_add" in Path(shim_src).read_text()
