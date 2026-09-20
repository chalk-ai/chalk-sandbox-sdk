"""Source-context tracebacks using only the standard library.

Keep Python's exception-chain, note, group, and repeated-frame formatting;
only replace the presentation of individual stack frames. No exception hooks
are installed and local variable values are never captured.
"""

from __future__ import annotations

import linecache
import traceback
from types import TracebackType


def format_frame(frame: traceback.FrameSummary) -> str:
    """Render a location with three source lines on either side when available."""
    lines = [f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}\n']
    source = linecache.getlines(frame.filename)
    lineno = frame.lineno
    if lineno is not None and 1 <= lineno <= len(source):
        start, end = max(1, lineno - 3), min(len(source), lineno + 3)
        width = len(str(end))
        for number in range(start, end + 1):
            marker = ">" if number == lineno else " "
            text = source[number - 1].rstrip("\r\n").expandtabs(4)
            lines.append(f"  {marker} {number:>{width}} | {text}\n")
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
                lines.append(f"    {'':>{width}} | {underline}\n")
    elif frame.line:
        # Extracted tracebacks may retain a line after a file has disappeared.
        lines.append(f"  > {lineno} | {frame.line}\n")
    return "".join(lines) + "\n"


class _ContextStack(traceback.StackSummary):
    def format_frame_summary(
        self, frame_summary: traceback.FrameSummary, **kwargs: object
    ) -> str:
        # Python 3.13+ passes colorize. Keep log content plain for all handlers.
        return format_frame(frame_summary)


def format_exception(
    error: BaseException, tb: TracebackType | None, *, compact: bool = False
) -> str:
    """Format an error, optionally omitting plumbing for expected failures."""
    summary = traceback.TracebackException(
        type(error), error, tb, limit=0 if compact else None, capture_locals=False
    )
    pending = [summary]
    while pending:
        current = pending.pop()
        current.stack = _ContextStack(current.stack)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
        if current.exceptions:
            pending.extend(current.exceptions)
    return "".join(summary.format())
