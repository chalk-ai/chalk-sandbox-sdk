"""Definition positions in captured source, after deployment transforms."""

from __future__ import annotations

import ast

from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import service_pb as catalog_pb


def function_source_location(
    source: str, path: str, qualified_name: str | None
) -> catalog_pb.FunctionSourceLocation | None:
    if qualified_name is None:
        return None
    scope = ast.parse(source).body
    definition = None
    for name in qualified_name.split("."):
        if name == "<locals>":
            continue
        matches = [
            node
            for node in scope
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == name
        ]
        # Conditional or repeated definitions need runtime identity, not a guessed match.
        if len(matches) != 1:
            return None
        definition = matches[0]
        scope = definition.body
    if not isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    line = source.splitlines()[definition.lineno - 1]
    prefix = line.encode("utf-8")[: definition.col_offset].decode("utf-8")
    return catalog_pb.FunctionSourceLocation(
        path=path,
        line_number=definition.lineno,
        column_number=len(prefix.encode("utf-16-le")) // 2 + 1,
    )
