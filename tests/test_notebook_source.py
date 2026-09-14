from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from chalkcompute._function import FunctionBuildError, RemoteFunction
from chalkcompute._notebook import _bundle_notebook_source, notebook_source_for_function


def _cells(*sources: str) -> list[str]:
    return [textwrap.dedent(source).strip() for source in sources]


def test_transitive_imports_and_helpers_run_in_fresh_interpreter(
    tmp_path: Path,
) -> None:
    source = _bundle_notebook_source(
        _cells(
            'import math as m; print("DO NOT REPLAY")',
            "offset = 2\nunrelated = expensive_query()",
            "def helper(x):\n    return m.sqrt(x) + offset",
            "def score(x):\n    return helper(x)",
        ),
        "score",
    )
    assert "expensive_query" not in source
    assert "print" not in source
    module = tmp_path / "user_module.py"
    module.write_text(source + "\nassert score(9) == 5\n")
    result = subprocess.run(
        [sys.executable, str(module)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_latest_global_and_original_default_bindings_are_both_preserved() -> None:
    source = _bundle_notebook_source(
        _cells(
            "factor = 2",
            "def helper(x=factor):\n    return x * factor",
            "factor = 3",
            "def score():\n    return helper()",
        ),
        "score",
    )
    namespace: dict[str, Any] = {}
    exec(source, namespace)
    assert namespace["score"]() == 6


def test_shadowed_helper_excludes_old_dependencies() -> None:
    source = _bundle_notebook_source(
        _cells(
            "def helper(x):\n    return unavailable(x)",
            "def helper(x):\n    return x + 1",
            "def score(x):\n    return helper(x)",
        ),
        "score",
    )
    assert "unavailable" not in source
    namespace: dict[str, Any] = {}
    exec(source, namespace)
    assert namespace["score"](2) == 3


def test_missing_runtime_value_has_actionable_error() -> None:
    with pytest.raises(ValueError, match="cannot find source for: dataframe"):
        _bundle_notebook_source(["def score():\n    return len(dataframe)"], "score")


def test_kernel_injection_and_control_flow_are_not_packaged() -> None:
    with pytest.raises(ValueError, match="kernel-only state"):
        _bundle_notebook_source(
            _cells(
                "__chalk_rows = [1, 2]",
                "rows = __chalk_rows",
                "def score():\n    return len(rows)",
            ),
            "score",
        )
    with pytest.raises(ValueError, match="executable control flow"):
        _bundle_notebook_source(
            _cells(
                "for x in range(3):\n    total = x",
                "def score():\n    return total",
            ),
            "score",
        )


def test_future_imports_remain_at_module_start() -> None:
    source = _bundle_notebook_source(
        _cells(
            "x = 1",
            "from __future__ import annotations",
            "def score(value: int) -> int:\n    return value + x",
        ),
        "score",
    )
    assert source.startswith("from __future__ import annotations\n")
    namespace: dict[str, Any] = {}
    exec(source, namespace)
    assert namespace["score"](2) == 3


def test_unrelated_failed_and_magic_cells_do_not_block_deployment() -> None:
    source = _bundle_notebook_source(
        _cells(
            "def incomplete(",
            'get_ipython().run_line_magic("pip", "install example")',
            "import math",
            "async def score(value):\n    return math.floor(value)",
        ),
        "score",
    )
    assert "get_ipython" not in source
    assert "import math" in source


@pytest.fixture
def shell(monkeypatch: pytest.MonkeyPatch):
    import IPython
    from IPython.core.interactiveshell import InteractiveShell

    instance = InteractiveShell()
    monkeypatch.setattr(IPython, "get_ipython", lambda: instance)
    yield instance
    instance.history_manager.end_session()


@pytest.mark.parametrize("strip", ["0", "1"])
def test_notebook_decorator_freezes_dependencies_before_background_deploy(
    shell: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    strip: str,
) -> None:
    monkeypatch.setenv("CHALK_FUNCTION_STRIP_SOURCE", strip)
    monkeypatch.setattr(RemoteFunction, "_deploy", lambda self, **kwargs: self)
    result = shell.run_cell("import math as m\nimport chalkcompute", store_history=True)
    assert result.success
    result = shell.run_cell(
        "offset = 2\ndef helper(x):\n    return m.sqrt(x) + offset", store_history=True
    )
    assert result.success
    result = shell.run_cell(
        '@chalkcompute.function()\ndef score(x: float) -> float:\n    return helper(x)\nprint("local only")',
        store_history=True,
    )
    assert result.success
    fn = shell.user_ns["score"]
    fn._wait_for_background_deploy()
    snapshot = fn._notebook_source
    assert snapshot is not None
    assert "local only" not in snapshot
    shell.run_cell("offset = 100", store_history=True)
    assert fn._notebook_source == snapshot

    # Exercise the source path consumed by the real image builder, including
    # both the released SDK's no-op decorators and local source-strip mode.
    class CaptureImage:
        def __init__(self) -> None:
            super().__init__()
            self.source = ""

        def add_local_file(self, source: str, destination: str) -> "CaptureImage":
            self.source = Path(source).read_text()
            return self

    image = CaptureImage()
    temp_files: list[str] = []
    try:
        fn._add_single_file_source(image, temp_files, "user_module")
        path = tmp_path / "user_module.py"
        path.write_text(image.source)
        monkeypatch.setenv("CHALK_REMOTE_FUNCTION", "1")
        spec = importlib.util.spec_from_file_location("notebook_test_module", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.score(9) == 5
    finally:
        for filename in temp_files:
            Path(filename).unlink(missing_ok=True)


def test_imported_file_function_keeps_file_deployment_in_notebook(shell: Any) -> None:
    assert notebook_source_for_function(_cells) is None


def test_nested_notebook_function_reports_error(shell: Any) -> None:
    shell.run_cell(
        "def factory():\n    def nested(x):\n        return x\n    return nested\nfn = factory()",
        store_history=True,
    )
    with pytest.raises(ValueError, match="top-level function"):
        notebook_source_for_function(shell.user_ns["fn"])


def test_missing_dependency_fails_before_scheduling(
    shell: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    monkeypatch.setattr(
        RemoteFunction, "_start_background_deploy", lambda self: calls.append(self)
    )
    shell.run_cell(
        "def score(x: float) -> float:\n    return missing(x)", store_history=True
    )
    with pytest.raises(FunctionBuildError, match="missing"):
        RemoteFunction(shell.user_ns["score"])
    assert calls == []
