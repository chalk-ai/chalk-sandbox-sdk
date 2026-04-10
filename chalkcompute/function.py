"""
Decorator for deploying Python functions as remote scaling groups.

Usage:
    import chalkcompute

    @chalkcompute.function()
    def add(x: int, y: int) -> int:
        return x + y

    # The scaling group is deployed when the module loads.
    # Calling the function makes an HTTP request to the remote endpoint.
    result = add(1, 2)

Deployed as two files:

    /app/user_module.py  — the user's source file, with chalkcompute
                           imports and decorators stripped out
    /app/handler.py      — a shim that imports the function and wraps it:

        from user_module import add

        def handler(event, context):
            return add(**event)
"""

from __future__ import annotations

import ast
import functools
import inspect
import os
import tempfile
import types
import uuid
from pathlib import Path
from typing import Any, Callable

from chalkcompute.image import Image
from chalkcompute.scaling_group import ScalingGroup

_REMOTE_ENV_VAR = "_CHALK_REMOTE_FUNCTION"


# ---------------------------------------------------------------------------
# Source transformation helpers
# ---------------------------------------------------------------------------


def _strip_chalkcompute_from_source(source: str) -> str:
    """Remove chalkcompute imports and @chalkcompute.function decorators.

    Preserves all other code (helpers, constants, classes, non-chalkcompute
    imports) so the deployed module is self-contained minus the SDK.
    """
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)

    # Collect line ranges to remove (1-indexed, inclusive)
    remove_ranges: list[tuple[int, int]] = []

    for node in ast.iter_child_nodes(tree):
        # Remove: import chalkcompute / from chalkcompute import ...
        if isinstance(node, ast.Import):
            if any(alias.name.startswith("chalkcompute") for alias in node.names):
                remove_ranges.append((node.lineno, node.end_lineno or node.lineno))
                continue
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("chalkcompute"):
                remove_ranges.append((node.lineno, node.end_lineno or node.lineno))
                continue

        # Remove @chalkcompute.function(...) decorators from function defs
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                dec_source = ast.get_source_segment(source, dec)
                if dec_source and "chalkcompute" in dec_source:
                    # Remove the decorator line(s) including the @ symbol.
                    # The @ is on the line before the decorator node in some
                    # cases, so we start from the decorator's lineno which
                    # already accounts for the @ in Python 3.8+.
                    remove_ranges.append((dec.lineno, dec.end_lineno or dec.lineno))

    # Build output, skipping removed line ranges
    removed_lines: set[int] = set()
    for start, end in remove_ranges:
        for ln in range(start, end + 1):
            removed_lines.add(ln)

    kept = [line for i, line in enumerate(lines, start=1) if i not in removed_lines]
    return "".join(kept)


def _generate_handler_shim(func_name: str) -> str:
    """Generate a tiny handler.py that imports the function and wraps it."""
    return (
        f"from user_module import {func_name}\n"
        "\n"
        "\n"
        "def handler(event, context):\n"
        f"    return {func_name}(**event)\n"
    )


def _write_temp(content: str) -> str:
    """Write content to a temp file and return its path."""
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", prefix="chalkfn_", delete=False,
    )
    f.write(content)
    f.close()
    return f.name


# ---------------------------------------------------------------------------
# RemoteFunction
# ---------------------------------------------------------------------------


class RemoteFunction:
    """A function deployed to a remote ScalingGroup."""

    def __init__(
        self,
        func: Callable[..., Any],
        scaling_group: ScalingGroup,
    ) -> None:
        self._func = func
        self._scaling_group = scaling_group
        functools.update_wrapper(self, func)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        sig = inspect.signature(self._func)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        payload = dict(bound.arguments)

        response = self._scaling_group.call(
            "/",
            method="POST",
            json=payload,
        )
        response.raise_for_status()
        result = response.json()
        return result.get("result", result)

    @property
    def scaling_group(self) -> ScalingGroup:
        return self._scaling_group

    def delete(self) -> None:
        self._scaling_group.delete()


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


def function(
    *,
    image: Image | None = None,
    cpu: str | None = None,
    memory: str | None = None,
    gpu: str | None = None,
    name: str | None = None,
    env: dict[str, str] | None = None,
    min_replicas: int = 1,
    max_replicas: int = 1,
) -> Callable[[types.FunctionType], RemoteFunction | types.FunctionType]:
    """Decorator that deploys a function as a remote scaling group.

    **Locally** (at decoration time):
    1. AST-strips the source file (removes chalkcompute imports/decorators)
    2. Generates a handler shim that imports and wraps the function
    3. Builds an image with both files + ``chalk-remote-call-python``
    4. Creates a ScalingGroup and calls ``deploy()``

    **Remotely** (inside the container):
    The decorator is a no-op — returns the raw function unchanged.
    """

    def decorator(func: types.FunctionType) -> RemoteFunction | types.FunctionType:
        # When running inside a deployed container, skip deploy entirely.
        if os.environ.get(_REMOTE_ENV_VAR):
            return func

        base_image = image if image is not None else Image.debian_slim()

        # Strip chalkcompute from the user's source file
        source_file = inspect.getfile(func)
        source = Path(source_file).read_text()
        stripped_source = _strip_chalkcompute_from_source(source)
        handler_shim = _generate_handler_shim(func.__name__)

        # Write both to temp files for add_local_file
        stripped_path = _write_temp(stripped_source)
        shim_path = _write_temp(handler_shim)

        deploy_image = (
            base_image.pip_install(["chalk-remote-call-python"])
            .add_local_file(stripped_path, "/app/user_module.py", strategy="copy")
            .add_local_file(shim_path, "/app/handler.py", strategy="copy")
            .workdir("/app")
            .env({"PYTHONPATH": "/app", _REMOTE_ENV_VAR: "1"})
            .entrypoint(["chalk-remote-call", "--handler", "handler.handler"])
        )

        sg = ScalingGroup(
            image=deploy_image,
            name=name or f"fn-{func.__name__}-{uuid.uuid4().hex[:8]}",
            cpu=cpu,
            memory=memory,
            gpu=gpu,
            env={
                "CHALK_CLIENT_ID": os.environ.get("CHALK_CLIENT_ID", ""),
                "CHALK_CLIENT_SECRET": os.environ.get("CHALK_CLIENT_SECRET", ""),
                "CHALK_ENVIRONMENT_ID": os.environ.get("CHALK_ENVIRONMENT_ID", ""),
                **(env or {}),
            },
            port=8080,
            protocol="grpc",
            min_replicas=min_replicas,
            max_replicas=max_replicas,
        )
        sg.deploy()

        # Clean up temp files after image build has consumed them
        os.unlink(stripped_path)
        os.unlink(shim_path)

        return RemoteFunction(func, sg)

    return decorator
