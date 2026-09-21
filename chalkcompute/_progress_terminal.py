"""Optional live terminal presentation; deployment state lives in _progress.

Rich owns cursor movement, wrapping and terminal sizing. Application writes are
forwarded verbatim to their original streams, after removing the live region.
We deliberately decline streams/loggers we cannot safely coordinate with.
"""

from __future__ import annotations

import io
import logging
import os
import sys
from collections.abc import Iterable
from typing import TYPE_CHECKING, TextIO, cast

from chalkcompute._terminal import interactive_terminal

if TYPE_CHECKING:
    from threading import RLock

    from rich.console import Console
    from rich.live import Live
    from rich.text import Text


def _terminal_id(stream: TextIO) -> tuple[int, int] | None:
    # A pseudo-TTY backed IDE terminal is fine; a notebook/debugger/capture
    # wrapper claiming isatty() is not sufficient evidence for cursor controls.
    while isinstance(stream, _OutputStream):
        stream = stream._stream
    if not isinstance(stream, io.TextIOWrapper) or not interactive_terminal(stream):
        return None
    try:
        stat = os.fstat(stream.fileno())
        return (stat.st_dev, stat.st_rdev) if os.isatty(stream.fileno()) else None
    except (OSError, ValueError):
        return None


def _has_terminal_handler(terminal: tuple[int, int]) -> bool:
    # Existing handlers retain their stream, so replacing sys.stderr would not
    # intercept them. Do not change the application's logging configuration.
    loggers = [logging.getLogger(), *logging.Logger.manager.loggerDict.values()]
    for logger in loggers:
        if not isinstance(logger, logging.Logger):
            continue
        for handler in logger.handlers:
            stream = getattr(handler, "stream", None)
            if stream is not None and _terminal_id(stream) == terminal:
                return True
    return False


class _OutputStream:
    """Forward text unchanged and delegate stream attributes to the original."""

    def __init__(self, display: LiveDisplay, stream: TextIO) -> None:
        self._display = display
        self._stream = stream

    def __getattr__(self, name: str):
        return getattr(self._stream, name)

    def write(self, text: str) -> int:
        with self._display.lock:
            if self._display.active and text:
                try:
                    self._display.suspend()
                except (OSError, ValueError):
                    # Rendering must not prevent an application's write.
                    try:
                        self._display.close()
                    except (OSError, ValueError):
                        pass
            written = self._stream.write(text)
            if self._display.active and text:
                self._display.line_start = text.endswith("\n")
                if self._display.line_start:
                    # Flush stdout before drawing on stderr, preserving ordering
                    # even when the application configured buffered terminal IO.
                    self._stream.flush()
                    try:
                        self._display.resume()
                    except (OSError, ValueError):
                        try:
                            self._display.close()
                        except (OSError, ValueError):
                            pass
            return written

    def flush(self) -> None:
        self._stream.flush()

    def writelines(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.write(line)


class LiveDisplay:
    def __init__(self, console: Console, stream: TextIO, lock: RLock) -> None:
        self.console = console
        self.stream = stream
        self.lock = lock
        self.active = True
        self.line_start = True
        self.live: Live | None = None
        self.rows: Text | None = None
        self.streams: dict[str, tuple[TextIO, TextIO]] = {}

    @classmethod
    def create(cls, stream: TextIO, lock: RLock) -> LiveDisplay | None:
        if any(
            os.environ.get(name) == "0"
            for name in ("TTY_INTERACTIVE", "TTY_COMPATIBLE")
        ):
            return None
        if isinstance(stream, _OutputStream) and stream._display.active:
            return None  # Another tracker already owns this terminal.
        terminal = _terminal_id(stream)
        if terminal is None or _has_terminal_handler(terminal):
            return None
        if os.name != "nt" and not os.environ.get("TERM"):
            return None
        try:
            from rich.console import Console
        except ImportError:
            return None  # Minimal/local SDK copies can still print plain status.
        console = Console(
            file=stream,
            force_terminal=True,
            force_jupyter=False,
            markup=False,
            highlight=False,
            no_color="NO_COLOR" in os.environ,
        )
        if console.legacy_windows or not console.is_interactive:
            return None
        display = cls(console, stream, lock)
        for name in ("stdout", "stderr"):
            original = getattr(sys, name)
            if _terminal_id(original) == terminal:
                proxy = cast(TextIO, _OutputStream(display, original))
                display.streams[name] = (original, proxy)
                setattr(sys, name, proxy)
        return display

    def owns_streams(self) -> bool:
        return all(
            getattr(sys, name) is proxy for name, (_, proxy) in self.streams.items()
        )

    def update(self, lines: list[str]) -> None:
        from rich.text import Text

        # ANSI is generated only by our renderer; labels are sanitized upstream.
        self.rows = Text.from_ansi("\n".join(lines))
        self.resume()

    def resume(self) -> None:
        from rich.live import Live

        if not self.active or not self.line_start or self.rows is None:
            return  # Never overwrite an application's unfinished output line.
        if self.live is None:
            self.live = Live(
                self.rows,
                console=self.console,
                auto_refresh=False,
                redirect_stdout=False,
                redirect_stderr=False,
                vertical_overflow="ellipsis",
            )
            self.live.start(refresh=True)
        else:
            self.live.update(self.rows, refresh=True)

    def suspend(self) -> None:
        if self.live is not None:
            live, self.live = self.live, None
            live.transient = True
            live.stop()

    def close(self) -> None:
        if not self.active:
            return
        self.active = False
        try:
            if self.live is not None:
                live, self.live = self.live, None
                live.stop()
            elif self.rows is not None:
                if not self.line_start:
                    self.console.file.write("\n")
                self.console.print(self.rows)
        finally:
            # Respect redirect_stderr(), notebook capture, or user changes made
            # while deployment was running. Retained proxies become pass-through.
            for name, (original, proxy) in self.streams.items():
                if getattr(sys, name) is proxy:
                    setattr(sys, name, original)
