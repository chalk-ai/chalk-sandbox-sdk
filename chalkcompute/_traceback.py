"""Source-context tracebacks using only the standard library.

Keep Python's exception-chain, note, group, and repeated-frame formatting;
only replace the presentation of individual stack frames. No exception hooks
are installed and local variable values are never captured.
"""

from __future__ import annotations

import linecache
import re
import traceback
from types import TracebackType

from chalkcompute._terminal import styled


def format_frame(frame: traceback.FrameSummary, *, color: bool = False) -> str:
    """Render a location with three source lines on either side when available."""
    filename = styled(frame.filename, "36", color=color)
    location = styled(str(frame.lineno), "33", color=color)
    name = styled(frame.name, "35", color=color)
    lines = [f'  File "{filename}", line {location}, in {name}\n']
    source = linecache.getlines(frame.filename)
    lineno = frame.lineno
    if lineno is not None and 1 <= lineno <= len(source):
        start, end = max(1, lineno - 3), min(len(source), lineno + 3)
        width = len(str(end))
        for number in range(start, end + 1):
            marker = ">" if number == lineno else " "
            text = source[number - 1].rstrip("\r\n").expandtabs(4)
            gutter = styled(
                f"  {marker} {number:>{width}} |",
                "1;31" if number == lineno else "2",
                color=color,
            )
            if number == lineno:
                text = styled(text, "1;33", color=color)
            lines.append(f"{gutter} {text}\n")
            if (
                number == lineno
                and frame.end_lineno == lineno
                and frame.colno is not None
                and frame.end_colno is not None
            ):
                # Python's instruction positions are UTF-8 byte offsets, not
                # character offsets. Expand tabs after decoding the prefix.
                raw = source[number - 1].rstrip("\r\n").encode("utf-8")
                start_column = len(
                    raw[: frame.colno].decode("utf-8", errors="replace").expandtabs(4)
                )
                end_column = len(
                    raw[: frame.end_colno]
                    .decode("utf-8", errors="replace")
                    .expandtabs(4)
                )
                underline = " " * start_column + "^" * max(1, end_column - start_column)
                lines.append(
                    styled(f"    {'':>{width}} | {underline}", "1;31", color=color)
                    + "\n"
                )
    elif frame.line:
        # Extracted tracebacks may retain a line after a file has disappeared.
        lines.append(styled(f"  > {lineno} | {frame.line}", "1;33", color=color) + "\n")
    return "".join(lines) + "\n"


class _ContextStack(traceback.StackSummary):
    def __init__(self, frames: traceback.StackSummary, *, color: bool) -> None:
        super().__init__(frames)
        self._color = color

    def format_frame_summary(
        self, frame_summary: traceback.FrameSummary, **kwargs: object
    ) -> str:
        # Python 3.13+ passes colorize. Use our stream's policy on every version.
        return format_frame(frame_summary, color=self._color)


def format_exception(
    error: BaseException,
    tb: TracebackType | None,
    *,
    compact: bool = False,
    color: bool = False,
) -> str:
    """Format an error, optionally omitting plumbing for expected failures."""
    summary = traceback.TracebackException(
        type(error), error, tb, limit=0 if compact else None, capture_locals=False
    )
    pending = [summary]
    exception_names: set[str] = set()
    while pending:
        current = pending.pop()
        current.stack = _ContextStack(current.stack, color=color)
        if color:
            # exc_type_str replaces the deprecated exc_type on Python 3.13+.
            type_name = getattr(current, "exc_type_str", None)
            if isinstance(type_name, str):
                exception_names.add(type_name)
            elif current.exc_type is not None:
                kind = current.exc_type
                name = kind.__qualname__
                if kind.__module__ not in ("builtins", "__main__"):
                    name = f"{kind.__module__}.{name}"
                exception_names.add(name)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
        if current.exceptions:
            pending.extend(current.exceptions)
    text = "".join(summary.format())
    if exception_names:
        # Only color exception labels, leaving messages, notes, and recovery
        # commands literal. Exception groups prefix these lines with gutters.
        names = "|".join(re.escape(name) for name in sorted(exception_names))
        text = re.sub(
            rf"(?m)^([ |]*)({names})(?=:|$)",
            lambda match: match[1] + styled(match[2], "1;31", color=True),
            text,
        )
    return text
