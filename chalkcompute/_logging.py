"""Opt-in console logging and shared deployment status diagnostics.

Importing the SDK leaves Python's logging configuration alone. In an application,
configure the ``chalkcompute`` logger using the application's normal handlers.
Small scripts and notebooks can use :func:`configure_logging` instead.
"""

from __future__ import annotations

import copy
import logging
import threading
from types import TracebackType
from typing import TextIO

from chalkcompute._terminal import styled, supports_color
from chalkcompute._traceback import format_exception

_configuration_lock = threading.Lock()
_console_handler: logging.Handler | None = None


def configure_logging(
    level: int | str = logging.INFO, *, stream: TextIO | None = None, color: bool = True
) -> None:
    """Enable SDK console logs (stderr by default).

    Only the ``chalkcompute`` logger is configured. Records use literal text,
    with colored level labels and tracebacks on interactive terminals and plain
    text elsewhere.
    There is no markup interpretation, table wrapping, or cursor rewriting. Calling
    this again replaces only the handler installed by this helper, which is useful
    when rerunning notebook cells. Existing application handlers are left intact.

    This opts out of propagation to the root logger to avoid duplicate console
    output. Applications with their own logging configuration should configure
    ``logging.getLogger("chalkcompute")`` directly instead of using this helper.
    Use ``"DEBUG"`` for individual polls and transport retries, or ``"WARNING"``
    for problems only. Diagnostic logging and deployment progress are independent.
    Set ``CHALK_SANDBOX_TUI=false`` to disable progress if you want logs alone.

    Parameters
    ----------
    level
        Standard Python logging level (integer or uppercase name).
    stream
        Optional text stream; defaults to the current ``sys.stderr``.
    color
        Allow terminal color. Set to ``False`` to disable it. Redirected output,
        notebooks, CI, ``TERM=dumb``, and ``NO_COLOR`` always disable color.
    """
    global _console_handler
    handler = logging.StreamHandler(stream)
    handler.setLevel(level)  # Validate before changing any existing configuration.
    handler.setFormatter(_ConsoleFormatter(handler.stream, color=color))
    with _configuration_lock:
        logger = logging.getLogger("chalkcompute")
        if _console_handler is not None:
            logger.removeHandler(_console_handler)
            _console_handler.close()
        logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = False
        _console_handler = handler


class _ConsoleFormatter(logging.Formatter):
    def __init__(self, stream: TextIO, *, color: bool) -> None:
        super().__init__("%(levelname)s %(name)s: %(message)s")
        self._stream = stream
        self._color = color

    def format(self, record: logging.LogRecord) -> str:
        # Formatter caches exc_text on the record. Work on a copy so handlers
        # before and after ours retain their own exception presentation.
        record = copy.copy(record)
        if record.exc_info:
            record.exc_text = None
        text = super().format(record)
        if not self._color or not supports_color(self._stream):
            return text
        if record.levelno >= logging.ERROR:
            code = "31"
        elif record.levelno >= logging.WARNING:
            code = "33"
        elif record.levelno >= logging.INFO:
            code = "36"
        else:
            code = "2"
        # Style the formatted prefix without mutating the record shared with
        # application handlers (file/JSON handlers must never inherit our ANSI).
        return (
            styled(record.levelname, code, color=True) + text[len(record.levelname) :]
        )

    def formatException(
        self,
        ei: tuple[type[BaseException], BaseException, TracebackType | None]
        | tuple[None, None, None],
    ) -> str:
        if ei[1] is None:
            return super().formatException(ei)
        return format_exception(
            ei[1], ei[2], color=self._color and supports_color(self._stream)
        ).rstrip()


class _StatusLogger:
    """Log transitions at INFO and repeated polls at DEBUG, per operation."""

    def __init__(
        self, logger: logging.Logger, resource: str, *, progress_key: str | None = None
    ) -> None:
        self._logger = logger
        self._resource = resource
        self._progress_key = progress_key
        self._previous: tuple[str, int | None, int | None, str] | None = None

    def update(
        self,
        status: str,
        *,
        ready: int | None = None,
        available: int | None = None,
        message: str = "",
    ) -> None:
        state = (status, ready, available, message)
        detail = status
        if ready is not None:
            detail += f" ready={ready}"
        if available is not None:
            detail += f" available={available}"
        if message:
            detail += f" — {message}"
        level = logging.DEBUG if state == self._previous else logging.INFO
        self._logger.log(level, "%s: %s", self._resource, detail)
        if self._progress_key is not None and state != self._previous:
            from chalkcompute._progress import progress_update

            progress_update(self._progress_key, detail=detail)
        self._previous = state
