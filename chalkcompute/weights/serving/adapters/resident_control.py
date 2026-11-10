"""Content-pinned layout handoff shared by opt-in resident engine bridges."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any

MAX_LAYOUT_BYTES = 1024 * 1024


def digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def parse_layout(raw: bytes, expected_file_sha256: str) -> Any:
    from chalkcompute.weights.layout import WeightLayout

    if (
        not raw
        or len(raw) > MAX_LAYOUT_BYTES
        or not digest(expected_file_sha256)
        or hashlib.sha256(raw).hexdigest() != expected_file_sha256
    ):
        raise ValueError("Exact bounded layout bytes and SHA256 required")
    layout = WeightLayout.from_json(raw)
    if not digest(layout.source_schema_sha256):
        raise ValueError("Layout must be explicitly bound to its source schema")
    return layout


class LayoutSnapshot:
    """Read once before engine launch; later path mutations cannot change binding."""

    def __init__(self, path: Path, expected_file_sha256: str):
        path = Path(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or not 0 < info.st_size <= MAX_LAYOUT_BYTES
            ):
                raise ValueError("Bounded regular layout file required")
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                raw = source.read(MAX_LAYOUT_BYTES + 1)
        finally:
            os.close(descriptor)
        layout = parse_layout(raw, expected_file_sha256)
        self.raw = raw
        self.file_sha256 = expected_file_sha256
        self.layout_sha256 = layout.sha256
        self.schema_sha256 = layout.source_schema_sha256

    def check_manifest(self, manifest: Any) -> None:
        from chalkcompute.weights.layout import WeightSchema

        if WeightSchema.from_manifest(manifest).sha256 != self.schema_sha256:
            raise ValueError("Initial keyframe schema differs from pinned layout")

    def check_ack(self, result: dict[str, Any]) -> bool:
        return (
            result.get("layout_sha256") == self.layout_sha256
            and result.get("schema_sha256") == self.schema_sha256
        )
