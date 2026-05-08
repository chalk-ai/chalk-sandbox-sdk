from __future__ import annotations

import ast
from pathlib import Path

import chalkcompute


def _stub_exports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    exports: set[str] = set()

    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module.startswith("chalkcompute."):
                for alias in node.names:
                    exports.add(alias.asname or alias.name)
        elif isinstance(node, ast.FunctionDef):
            exports.add(node.name)

    return exports


def _stub_all(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        ):
            continue
        if not isinstance(node.value, ast.List):
            raise AssertionError("__all__ must be a list literal")
        return {
            elt.value
            for elt in node.value.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        }

    raise AssertionError("__all__ missing from chalkcompute/__init__.pyi")


def test_init_stub_matches_lazy_exports() -> None:
    init_path = Path(chalkcompute.__file__).resolve()
    stub_path = init_path.with_suffix(".pyi")

    assert _stub_exports(stub_path) == set(chalkcompute.__all__)
    assert _stub_all(stub_path) == set(chalkcompute.__all__)
