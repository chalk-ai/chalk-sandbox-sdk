"""Small, standard-library-only helpers for terminal presentation."""

from __future__ import annotations

import os
import sys
from typing import TextIO


def interactive_terminal(stream: TextIO) -> bool:
    """Whether this stream is an interactive terminal rather than captured output."""
    try:
        return bool(
            getattr(stream, "isatty", lambda: False)()
            and os.environ.get("TERM", "").lower() not in ("dumb", "unknown")
            and not any(
                os.environ.get(name)
                for name in ("CI", "BUILDKITE", "GITHUB_ACTIONS", "TF_BUILD")
            )
            and "ipykernel" not in sys.modules
        )
    except (OSError, ValueError):
        return False


def supports_color(stream: TextIO) -> bool:
    return interactive_terminal(stream) and "NO_COLOR" not in os.environ


def styled(text: str, code: str, *, color: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if color else text


def encodable(text: str, stream: TextIO) -> str:
    """Keep progress usable on terminals whose encoding cannot represent a label."""
    encoding = getattr(stream, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="backslashreplace").decode(encoding)
