"""
Rich TUI progress display for chalkcompute deployments.

Activated when ``CHALK_SANDBOX_TUI=true`` is set in the environment.
Shows a hierarchical spinner tree of all resources being deployed:
images, volumes, secrets, containers, functions, and classes.

Falls back to normal logger output when disabled or when Rich is not installed.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Literal

logger = logging.getLogger(__name__)

ResourceKind = Literal[
    "image",
    "volume",
    "secret",
    "container",
    "function",
    "class",
    "method",
]

StepStatus = Literal["pending", "active", "done", "failed", "skipped"]

_KIND_ICONS: dict[ResourceKind, str] = {
    "image": "📦",
    "volume": "💾",
    "secret": "🔑",
    "container": "🐳",
    "function": "⚡",
    "class": "🏗️",
    "method": "  ↳",
}

_KIND_VERBS: dict[ResourceKind, str] = {
    "image": "Building image",
    "volume": "Creating volume",
    "secret": "Resolving secret",
    "container": "Starting container",
    "function": "Deploying function",
    "class": "Deploying class",
    "method": "Registering method",
}


def tui_enabled() -> bool:
    """Check whether the Rich TUI progress display is enabled.

    Returns
    -------
    bool
        ``True`` when ``CHALK_SANDBOX_TUI`` is set to ``"true"`` or ``"1"``.
    """
    return os.environ.get("CHALK_SANDBOX_TUI", "").lower() in ("true", "1")


# ---------------------------------------------------------------------------
# Resource state
# ---------------------------------------------------------------------------


class _ResourceState:
    __slots__ = ("kind", "label", "status", "detail", "children")

    def __init__(self, kind: ResourceKind, label: str) -> None:
        self.kind = kind
        self.label = label
        self.status: StepStatus = "active"
        self.detail: str = ""
        self.children: list[_ResourceState] = []


# ---------------------------------------------------------------------------
# Global TUI display singleton
# ---------------------------------------------------------------------------


class DeployProgress:
    """Thread-safe global progress tracker rendered via Rich ``Live``.

    Call ``add`` to register a resource, ``update`` to change its
    status, and the display auto-refreshes. The Rich ``Live`` context is
    started lazily on the first ``add`` and stopped when ``finish``
    is called (or all resources reach a terminal state).
    """

    def __init__(self) -> None:
        self._resources: dict[str, _ResourceState] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._live: Any = None  # rich.live.Live | None
        self._started = False
        self._logger_suppressed = False

    # -- Public API --------------------------------------------------------

    def add(
        self,
        key: str,
        kind: ResourceKind,
        label: str,
        *,
        parent: str | None = None,
        status: StepStatus = "active",
        detail: str = "",
    ) -> None:
        """Register a resource in the progress tree.

        Parameters
        ----------
        key
            Unique identifier for the resource.
        kind
            Resource kind for icon selection.
        label
            Human-readable label shown next to the spinner.
        parent
            Optional key of an existing resource to nest under.
        status
            Initial status for the new row.
        detail
            Optional detail string rendered after the label.
        """
        with self._lock:
            # A completed display remains reusable so a concurrent caller that
            # obtained this singleton just before the previous operation finished
            # cannot add rows to an orphaned tracker. Only finished operations
            # are dropped: clearing wholesale would also discard the children of
            # a root that is still running, and later updates to those keys
            # would silently no-op instead of rendering.
            if not self._started:
                self._discard_finished_roots()
            state = _ResourceState(kind, label)
            state.status = status
            state.detail = detail
            self._resources[key] = state

            if parent and parent in self._resources:
                self._resources[parent].children.append(state)
            else:
                self._order.append(key)

            self._ensure_live()
            self._refresh()

    def update(
        self,
        key: str,
        *,
        status: StepStatus | None = None,
        detail: str | None = None,
        label: str | None = None,
    ) -> None:
        """Update a resource's status and/or detail.

        Parameters
        ----------
        key
            Resource identifier previously passed to ``add``.
        status
            New status, if changing.
        detail
            New detail string, if changing.
        label
            New label, if changing.
        """
        with self._lock:
            state = self._resources.get(key)
            if state is None:
                return
            if status is not None:
                state.status = status
            if detail is not None:
                state.detail = detail
            if label is not None:
                state.label = label
            self._refresh()

    def finish(self) -> None:
        """Stop the Live display and restore logger output."""
        with self._lock:
            self._stop_live()

    def finish_if_idle(self) -> None:
        """Stop the display only when every top-level operation is terminal."""
        with self._lock:
            if self._all_roots_terminal():
                self._stop_live()

    def _all_roots_terminal(self) -> bool:
        return all(
            self._resources[key].status in ("done", "failed", "skipped")
            for key in self._order
        )

    def _discard_finished_roots(self) -> None:
        """Drop rows belonging to top-level operations that already finished.

        A root counts as finished only when every row in its subtree is
        terminal. A root can reach ``failed`` while a child is still active
        (see ``finish_if_idle``), and that child's later update must still
        find its state rather than silently no-op.
        """
        stale: set[int] = set()
        finished: set[str] = set()
        for key in self._order:
            subtree: list[_ResourceState] = []
            pending = [self._resources[key]]
            while pending:
                state = pending.pop()
                if any(state is seen for seen in subtree):
                    continue
                subtree.append(state)
                pending.extend(state.children)
            if any(
                state.status not in ("done", "failed", "skipped") for state in subtree
            ):
                continue
            finished.add(key)
            stale.update(id(state) for state in subtree)

        if not finished:
            return

        self._order = [key for key in self._order if key not in finished]
        self._resources = {
            key: state
            for key, state in self._resources.items()
            if id(state) not in stale
        }

    # -- Rendering ---------------------------------------------------------

    def _render(self) -> Any:
        from rich.table import Table

        grid = Table.grid(padding=(0, 0))
        grid.add_column(width=3)  # icon
        grid.add_column(width=3)  # status
        grid.add_column()  # label + detail

        for key in self._order:
            state = self._resources[key]
            self._render_row(grid, state, indent=0)

        return grid

    def _render_row(self, grid: Any, state: _ResourceState, indent: int) -> None:
        from rich.spinner import Spinner
        from rich.table import Table
        from rich.text import Text

        icon_str = _KIND_ICONS.get(state.kind, " ")
        prefix = "  " * indent

        if state.status == "active":
            status_widget = Spinner("dots", style="cyan")
        elif state.status == "done":
            status_widget = Text("✓", style="bold green")
        elif state.status == "failed":
            status_widget = Text("✗", style="bold red")
        elif state.status == "skipped":
            status_widget = Text("–", style="dim")
        else:  # pending
            status_widget = Text("◌", style="dim")

        label = state.label
        if state.detail:
            label = f"{label} — {state.detail}"

        style = "dim" if state.status == "pending" else ""
        if state.status == "failed":
            style = "red"

        icon_col = Text(f"{prefix}{icon_str}")
        label_col = Text(f" {label}", style=style)

        # Build a tight status + label row
        row = Table.grid(padding=(0, 1))
        row.add_column(width=2)
        row.add_column()
        row.add_row(status_widget, label_col)

        grid.add_row(icon_col, Text(""), row)

        for child in state.children:
            self._render_row(grid, child, indent=indent + 1)

    # -- Live management ---------------------------------------------------

    def _ensure_live(self) -> None:
        """Start the Rich ``Live`` display if not already started."""
        if self._started:
            return
        try:
            from rich.live import Live

            self._live = Live(
                self._render(),
                refresh_per_second=12,
                transient=False,
            )
            self._live.start()
            self._started = True
            self._suppress_logger()
        except ImportError:
            pass

    def _refresh(self) -> None:
        if self._live is not None:
            try:
                self._live.update(self._render())
            except Exception:
                pass

    def _stop_live(self) -> None:
        if self._live is not None:
            try:
                self._live.update(self._render())
                self._live.stop()
            except Exception:
                pass
            self._live = None
        self._started = False
        self._restore_logger()

    # SDK logs can corrupt the live display while it is rendering.
    _SUPPRESSED_LOGGERS = ("chalkcompute",)

    def _suppress_logger(self) -> None:
        """Suppress noisy loggers while the TUI is active."""
        if self._logger_suppressed:
            return
        self._saved_levels: dict[str, int] = {}
        for name in self._SUPPRESSED_LOGGERS:
            lg = logging.getLogger(name)
            self._saved_levels[name] = lg.level
            lg.setLevel(logging.CRITICAL + 1)
        self._logger_suppressed = True

    def _restore_logger(self) -> None:
        """Restore logger levels saved before suppression."""
        if not self._logger_suppressed:
            return
        for name, level in self._saved_levels.items():
            logging.getLogger(name).setLevel(level)
        self._saved_levels = {}
        self._logger_suppressed = False


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_global_progress: DeployProgress | None = None
_global_lock = threading.Lock()


def get_progress() -> DeployProgress | None:
    """Return the global progress tracker.

    Returns
    -------
    DeployProgress | None
        The singleton tracker, or ``None`` if the TUI is disabled.
    """
    global _global_progress
    if not tui_enabled():
        return None
    with _global_lock:
        if _global_progress is None:
            _global_progress = DeployProgress()
        return _global_progress


def reset_progress() -> None:
    """Stop and discard the global progress tracker."""
    global _global_progress
    with _global_lock:
        if _global_progress is not None:
            _global_progress.finish()
            _global_progress = None


# ---------------------------------------------------------------------------
# Convenience helpers for common operations
# ---------------------------------------------------------------------------


def progress_add(
    key: str,
    kind: ResourceKind,
    label: str,
    *,
    parent: str | None = None,
    status: StepStatus = "active",
    detail: str = "",
) -> None:
    """Add a resource to the global progress display (no-op if TUI disabled).

    Parameters
    ----------
    key
        Unique identifier for the resource.
    kind
        Resource kind for icon selection.
    label
        Human-readable label.
    parent
        Optional parent row to nest under.
    status
        Initial status for the row.
    detail
        Optional detail string.
    """
    p = get_progress()
    if p is not None:
        p.add(key, kind, label, parent=parent, status=status, detail=detail)


def progress_update(
    key: str,
    *,
    status: StepStatus | None = None,
    detail: str | None = None,
    label: str | None = None,
) -> None:
    """Update a resource in the global progress display (no-op if TUI disabled).

    Parameters
    ----------
    key
        Resource identifier previously passed to ``progress_add``.
    status
        New status, if changing.
    detail
        New detail string, if changing.
    label
        New label, if changing.
    """
    p = get_progress()
    if p is not None:
        p.update(key, status=status, detail=detail, label=label)


def progress_done(key: str, *, detail: str = "") -> None:
    """Mark a resource as done in the global progress display.

    Parameters
    ----------
    key
        Resource identifier previously passed to ``progress_add``.
    detail
        Optional final detail string.
    """
    progress_update(key, status="done", detail=detail)


def progress_fail(key: str, *, detail: str = "") -> None:
    """Mark a resource as failed in the global progress display.

    Parameters
    ----------
    key
        Resource identifier previously passed to ``progress_add``.
    detail
        Optional final detail string.
    """
    progress_update(key, status="failed", detail=detail)


def progress_finish() -> None:
    """Stop the global display after all concurrent operations finish."""
    p = get_progress()
    if p is not None:
        p.finish_if_idle()


# Keep the old import name alive but unused (backwards compat)
from typing import Any  # noqa: E402
