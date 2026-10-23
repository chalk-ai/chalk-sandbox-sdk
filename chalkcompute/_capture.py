"""Policies and request metadata for durable function call capture."""

from __future__ import annotations

import ast
import json
import math
from dataclasses import dataclass
from typing import Any, get_args, get_type_hints

CAPTURE_OPTION = "chalk.capture"
SECRET_METADATA = "chalk.capture.secret"
INPUTS_HEADER = "x-chalk-capture-inputs"
OUTPUTS_HEADER = "x-chalk-capture-outputs"
VERSION_HEADER = "x-chalk-function-version-id"
CALL_ID_HEADER = "x-chalk-call-id"
PARENT_HEADER = "x-chalk-parent-call-id"


@dataclass(frozen=True)
class CapturePolicy:
    """Retain call metadata and sample permitted input/output values.

    Request overrides bypass sampling, but never capture values marked Secret.
    ``capture=False`` disables metadata and payload capture for the function.
    """

    sample_rate: float = 1.0
    inputs: bool = True
    outputs: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.sample_rate, bool) or not isinstance(
            self.sample_rate, int | float
        ):
            raise TypeError("sample_rate must be a number between 0 and 1")
        if not math.isfinite(self.sample_rate) or not 0 <= self.sample_rate <= 1:
            raise ValueError("sample_rate must be finite and between 0 and 1")
        if not isinstance(self.inputs, bool) or not isinstance(self.outputs, bool):
            raise TypeError("inputs and outputs must be bools")


def capture_options(
    options: Any, capture: bool | CapturePolicy | None
) -> dict[str, str]:
    result = dict(options)
    if CAPTURE_OPTION in result:
        raise ValueError("set capture= instead of the reserved chalk.capture option")
    if capture is None:
        return result
    if not isinstance(capture, bool | CapturePolicy):
        raise TypeError("capture must be a bool or CapturePolicy")
    policy = capture if isinstance(capture, CapturePolicy) else CapturePolicy()
    result[CAPTURE_OPTION] = json.dumps(
        {
            "enabled": capture is not False,
            "sample_rate": policy.sample_rate,
            "inputs": policy.inputs,
            "outputs": policy.outputs,
        },
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return result


def contains_secret(annotation: Any, seen: frozenset[type]) -> bool:
    """Opaque structured values are excluded as a whole if any field is secret."""
    from chalkcompute._secret import Secret

    if any(marker is Secret for marker in getattr(annotation, "__metadata__", ())):
        return True
    if any(contains_secret(arg, seen) for arg in get_args(annotation)):
        return True
    if not isinstance(annotation, type) or annotation in seen:
        return False
    # Pydantic moves Annotated metadata into FieldInfo rather than its type hints.
    fields = getattr(annotation, "model_fields", {})
    if any(Secret in getattr(field, "metadata", ()) for field in fields.values()):
        return True
    if not getattr(annotation, "__annotations__", None):
        return False
    return any(
        contains_secret(value, seen | {annotation})
        for value in get_type_hints(annotation, include_extras=True).values()
    )


def require_capture_runtime(options: dict[str, str]) -> None:
    """Refuse to deploy a capture-enabled image with a runtime that would ignore its policy."""
    if not json.loads(options.get(CAPTURE_OPTION, "{} ")).get("enabled", False):
        return
    import chalk_remote_call

    if getattr(chalk_remote_call, "CAPTURE_PROTOCOL_VERSION", 0) < 1:
        raise RuntimeError(
            "Call capture requires a newer chalk-remote-call-python runtime with capture protocol 1. Upgrade the runtime before deploying with capture enabled."
        )


def strip_secret_annotation_markers(source: str) -> str:
    """Keep wire annotations importable when a deployed module omits the SDK."""
    tree = ast.parse(source)
    sdk_names = {"chalkcompute"}
    secret_names = set()
    annotated_names = {"Annotated"}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            sdk_names.update(
                a.asname or a.name for a in node.names if a.name == "chalkcompute"
            )
        if isinstance(node, ast.ImportFrom) and node.module in {
            "typing",
            "typing_extensions",
        }:
            annotated_names.update(
                a.asname or a.name for a in node.names if a.name == "Annotated"
            )
        if isinstance(node, ast.ImportFrom) and node.module == "chalkcompute":
            secret_names.update(
                a.asname or a.name for a in node.names if a.name == "Secret"
            )
    marker_edits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript) or not isinstance(node.slice, ast.Tuple):
            continue
        if not (
            (isinstance(node.value, ast.Name) and node.value.id in annotated_names)
            or (
                isinstance(node.value, ast.Attribute) and node.value.attr == "Annotated"
            )
        ):
            continue
        for marker in node.slice.elts[1:]:
            is_secret = isinstance(marker, ast.Name) and marker.id in secret_names
            is_secret = is_secret or (
                isinstance(marker, ast.Attribute)
                and marker.attr == "Secret"
                and isinstance(marker.value, ast.Name)
                and marker.value.id in sdk_names
            )
            if is_secret:
                marker_edits.append(marker)
    source_lines = source.splitlines(keepends=True)
    for marker in sorted(
        marker_edits, key=lambda n: (n.lineno, n.col_offset), reverse=True
    ):
        if marker.lineno == marker.end_lineno:
            line = source_lines[marker.lineno - 1].encode()
            source_lines[marker.lineno - 1] = (
                line[: marker.col_offset]
                + b'"chalk.capture.secret"'
                + line[marker.end_col_offset :]
            ).decode()
    source = "".join(source_lines)
    return source
