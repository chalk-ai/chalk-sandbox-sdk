"""Opt-in console logging and shared deployment status diagnostics.

Importing the SDK leaves Python's logging configuration alone. In an application,
configure the ``chalkcompute`` logger using the application's normal handlers.
Small scripts and notebooks can use :func:`configure_logging` instead.
"""

from __future__ import annotations

import logging
import threading
from typing import TextIO

_configuration_lock = threading.Lock()
_console_handler: logging.Handler | None = None


def configure_logging(
    level: int | str = logging.INFO, *, stream: TextIO | None = None
) -> None:
    """Enable plain SDK console logs (stderr by default).

    Only the ``chalkcompute`` logger is configured. Records use literal text,
    without color, markup, table wrapping, or terminal control sequences. Calling
    this again replaces only the handler installed by this helper, which is useful
    when rerunning notebook cells. Existing application handlers are left intact.

    This opts out of propagation to the root logger to avoid duplicate console
    output. Applications with their own logging configuration should configure
    ``logging.getLogger("chalkcompute")`` directly instead of using this helper.
    Use ``"DEBUG"`` for individual polls and transport retries, or ``"WARNING"``
    for problems only. Configuring logging disables automatic progress; explicitly
    opt in with ``CHALK_SANDBOX_TUI=true`` if desired.

    Parameters
    ----------
    level
        Standard Python logging level (integer or uppercase name).
    stream
        Optional text stream; defaults to the current ``sys.stderr``.
    """
    global _console_handler
    handler = logging.StreamHandler(stream)
    handler.setLevel(level)  # Validate before changing any existing configuration.
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    with _configuration_lock:
        logger = logging.getLogger("chalkcompute")
        if _console_handler is not None:
            logger.removeHandler(_console_handler)
            _console_handler.close()
        logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = False
        _console_handler = handler


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
