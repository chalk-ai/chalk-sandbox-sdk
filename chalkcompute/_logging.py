"""Configure logging for the chalkcompute SDK.

Installs a rich console handler on the ``chalkcompute`` logger when the
``rich`` package is available, otherwise falls back to a plain
``logging.StreamHandler``.
"""

from __future__ import annotations

import logging

_configured = False


def setup_logging() -> None:
    """Attach a handler to the ``chalkcompute`` root logger (idempotent).

    Uses ``rich.logging.RichHandler`` when available, otherwise a plain
    ``logging.StreamHandler``. Safe to call multiple times; only the first
    call installs the handler.
    """
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger("chalkcompute")
    if root.handlers:
        return

    try:
        from rich.logging import RichHandler

        handler = RichHandler(
            show_time=True,
            show_path=False,
            markup=True,
            rich_tracebacks=True,
        )
    except ImportError:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))

    root.addHandler(handler)
    root.setLevel(logging.INFO)
