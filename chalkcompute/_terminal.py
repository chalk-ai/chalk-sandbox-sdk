"""Small, standard-library-only helpers for terminal presentation."""

from __future__ import annotations

import os
import sys
from typing import TextIO


def interactive_terminal(stream: TextIO) -> bool:
    """Whether this stream is an interactive terminal rather than captured output."""
    return bool(
        getattr(stream, "isatty", lambda: False)()
        and os.environ.get("TERM") != "dumb"
        and not os.environ.get("CI")
        and "ipykernel" not in sys.modules
    )


def supports_color(stream: TextIO) -> bool:
    return interactive_terminal(stream) and "NO_COLOR" not in os.environ


def styled(text: str, code: str, *, color: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if color else text


def encodable(text: str, stream: TextIO) -> str:
    """Keep progress usable on terminals whose encoding cannot represent a label."""
    encoding = getattr(stream, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="backslashreplace").decode(encoding)
