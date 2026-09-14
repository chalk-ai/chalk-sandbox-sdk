"""Capture a notebook function's source dependencies before background deployment."""

from __future__ import annotations

import ast
import inspect
import linecache
import sys
from collections.abc import Callable, Sequence
from typing import Any


def _statement_source(source: str, node: ast.stmt) -> str:
    text = ast.get_source_segment(source, node)
    if text is None:
        raise ValueError("Could not locate a notebook statement's source.")
    if (
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.decorator_list
    ):
        start = min(decorator.lineno for decorator in node.decorator_list)
        decorators = "\n".join(source.splitlines()[start - 1 : node.lineno - 1])
        return decorators + "\n" + text
    return text


def _bundle_notebook_source(cells: Sequence[str], function_name: str) -> str:
    from chalkcompute._native import notebook_source_dependencies

    statements: list[tuple[int, ast.stmt, str]] = []
    future_imports: list[str] = []
    for cell_number, source in enumerate(cells, start=1):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            # Failed or magic-only history entries need not block an unrelated
            # function. Missing providers are diagnosed by the shared planner.
            continue
        for node in tree.body:
            text = _statement_source(source, node)
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                if text not in future_imports:
                    future_imports.append(text)
                continue
            statements.append((cell_number, node, text))

    sources = [source for _, _, source in statements]
    # Treat statements as graph nodes so an import next to a plotting or remote
    # invocation does not pull that unrelated invocation into module startup.
    indexes = notebook_source_dependencies([*sources, function_name])
    parts = [*future_imports]
    for index in indexes:
        if index == len(statements):
            continue
        cell_number, node, source = statements[index]
        if not isinstance(
            node,
            (
                ast.Import,
                ast.ImportFrom,
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.Assign,
                ast.AnnAssign,
            ),
        ):
            raise ValueError(
                f"Notebook deployment depends on executable control flow in cell {cell_number}. "
                "Move the required initialization into a function or importable module."
            )
        if any(
            isinstance(child, ast.Name)
            and (child.id == "get_ipython" or child.id.startswith("__chalk_"))
            for child in ast.walk(node)
        ):
            raise ValueError(
                f"Notebook deployment depends on kernel-only state in cell {cell_number}. "
                "Pass notebook results as function arguments or use an explicit dataset or volume."
            )
        parts.append(f"# Notebook cell {cell_number}\n{source}")
    return "\n\n".join(parts) + "\n"


def notebook_source_for_function(func: Callable[..., Any]) -> str | None:
    """Return a source snapshot for IPython functions, or None for file modules.

    Dependencies follow executed history through the defining cell. This matches
    the namespace in which decoration happens without shipping unexecuted edits.
    """
    func = inspect.unwrap(func)
    code = getattr(func, "__code__", None)
    if code is None:
        return None
    ipython = sys.modules.get("IPython")
    if ipython is None:
        return None
    shell = ipython.get_ipython()
    if shell is None:
        return None
    cell_number = shell.compile._filename_map.get(code.co_filename)
    if cell_number is None:
        return None
    source = "".join(linecache.getlines(code.co_filename))
    if not source:
        raise ValueError(
            "Notebook function source is no longer in the Python source cache."
        )
    tree = ast.parse(source)
    target = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == func.__name__
            and min([node.lineno, *(d.lineno for d in node.decorator_list)])
            <= code.co_firstlineno
            <= node.lineno
        ),
        None,
    )
    if target is None:
        raise ValueError(
            "Notebook deployments require a top-level function definition. "
            "Move nested functions into a top-level definition or importable module."
        )
    # Code after the definition has not executed at decoration time. In
    # particular, calls following it must never be replayed by the remote import.
    source = "\n".join(source.splitlines()[: target.end_lineno])
    history = list(shell.history_manager.input_hist_parsed[1:cell_number])
    return _bundle_notebook_source([*history, source], func.__name__)
