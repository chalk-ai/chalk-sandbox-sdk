"""Dependency-free deployment status output, separate from application logging.

Print only changed steps, with optional terminal color. Never rewrite previous
lines, replace standard streams, or change application logging configuration.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Literal

from chalkcompute._terminal import (
    encodable,
    styled,
    supports_color,
)

ResourceKind = Literal[
    "image",
    "volume",
    "secret",
    "container",
    "scaling_group",
    "function",
    "class",
    "method",
]

StepStatus = Literal["pending", "active", "done", "failed", "skipped"]

_STATUS_MARKS: dict[StepStatus, tuple[str, str, str]] = {
    "pending": ("·", ".", "2"),
    "active": ("›", ">", "36"),
    "done": ("✓", "+", "32"),
    "failed": ("✗", "!", "31"),
    "skipped": ("–", "-", "2"),
}


def tui_enabled() -> bool:
    """Show status in every environment unless explicitly disabled.

    Terminal detection controls presentation only. Logging handlers and levels
    never determine whether deployment status is visible.
    """
    mode = os.environ.get("CHALK_SANDBOX_TUI", "auto").strip().lower()
    return mode in ("auto", "true", "1")


# ---------------------------------------------------------------------------
# Resource state
# ---------------------------------------------------------------------------


_TERMINAL_STATUSES: tuple[StepStatus, ...] = ("done", "failed", "skipped")


def _skip_unfinished_descendants(state: "_ResourceState") -> None:
    """Mark a terminal row's unfinished children as skipped.

    An operation that has finished — successfully or not — has no steps still
    running, so leaving descendants "active" would make the two terminality checks
    disagree about whether the tree is idle.
    """
    pending = list(state.children)
    while pending:
        child = pending.pop()
        if child.status not in _TERMINAL_STATUSES:
            child.status = "skipped"
        pending.extend(child.children)


class _ResourceState:
    __slots__ = ("kind", "label", "status", "detail", "children", "last_rendered")

    def __init__(self, kind: ResourceKind, label: str) -> None:
        self.kind = kind
        self.label = label
        self.status: StepStatus = "active"
        self.detail: str = ""
        self.children: list[_ResourceState] = []
        self.last_rendered: tuple[StepStatus, str, str, int] | None = None


# ---------------------------------------------------------------------------
# Global TUI display singleton
# ---------------------------------------------------------------------------


class DeployProgress:
    """Thread-safe deployment tracker that prints a line for each changed step.

    Completed lines remain in scrollback. There are no refresh threads, cursor
    controls, or stream proxies, so warnings and application output stay intact.
    """

    def __init__(self) -> None:
        self._resources: dict[str, _ResourceState] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._started = False
        self._current_group: _ResourceState | None = None
        self._current_label = ""

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
            Resource kind.
        label
            Human-readable label shown next to the status marker.
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

            self._start()
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
                if status in _TERMINAL_STATUSES:
                    _skip_unfinished_descendants(state)
            if detail is not None:
                state.detail = detail
            if label is not None:
                state.label = label
            self._refresh()

    def finish(self) -> None:
        """Finish the current group of operations."""
        with self._lock:
            self._stop()

    def finish_if_idle(self) -> None:
        """Stop the display only when every top-level operation is terminal.

        A terminal row's unfinished children are marked skipped as they are set
        (see :func:`_skip_unfinished_descendants`), so a terminal root always
        has a fully terminal subtree and this agrees with the check that
        :meth:`_discard_finished_roots` makes.
        """
        with self._lock:
            if self._all_roots_terminal():
                self._stop()

    def _all_roots_terminal(self) -> bool:
        return all(
            self._resources[key].status in _TERMINAL_STATUSES for key in self._order
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
            if any(state.status not in _TERMINAL_STATUSES for state in subtree):
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

    def _refresh(self) -> None:
        stream = sys.stdout
        color = supports_color(stream)
        lines: list[str] = []
        for key in self._order:
            root = self._resources[key]
            continued = root.last_rendered is not None
            changes: list[tuple[_ResourceState, int]] = []
            pending = [(root, 0)]
            while pending:
                state, depth = pending.pop()
                pending.extend((child, depth + 1) for child in reversed(state.children))
                snapshot = (state.status, state.label, state.detail, depth)
                if snapshot != state.last_rendered:
                    state.last_rendered = snapshot
                    changes.append((state, depth))
            if not changes:
                continue

            label = " ".join(root.label.split())
            if self._current_group is not root or self._current_label != label:
                heading = styled(label, "1", color=color)
                if continued:
                    heading += styled(" (continued)", "2", color=color)
                lines.append(encodable(heading, stream))
                self._current_group = root
                self._current_label = label

            # Close the group after its last child, including children skipped
            # when the parent fails. No step should appear below the end marker.
            if root.status in _TERMINAL_STATUSES:
                changes.sort(key=lambda item: item[0] is root)
            for state, depth in changes:
                detail = " ".join(state.detail.split())
                is_root = state is root
                terminal = is_root and state.status in _TERMINAL_STATUSES
                if is_root:
                    if terminal:
                        label = {
                            "done": "Done",
                            "failed": "Failed",
                            "skipped": "Skipped",
                        }[state.status]
                        if state.status == "done" and detail in ("ready", "running"):
                            label, detail = detail.capitalize(), ""
                    else:
                        if not detail:
                            continue  # The heading already introduces the operation.
                        label, detail = detail, ""
                else:
                    label = " ".join(state.label.split())
                mark, fallback, code = _STATUS_MARKS[state.status]
                if encodable(mark, stream) != mark:
                    mark = fallback
                branch = "└" if terminal else "│"
                if encodable(branch, stream) != branch:
                    branch = "`" if terminal else "|"
                prefix = "  " + styled(branch, "2", color=color) + " "
                prefix += "  " * max(0, depth - 1)
                line = prefix + styled(mark, code, color=color) + " " + label
                if detail:
                    separator = "—" if encodable("—", stream) == "—" else "-"
                    line += styled(f" {separator} {detail}", "2", color=color)
                lines.append(encodable(line, stream))
        if lines:
            try:
                stream.write("\n".join(lines) + "\n")
                stream.flush()
            except (OSError, ValueError):
                # A closed/redirected console must not fail a deployment.
                pass

    def _start(self) -> None:
        self._started = True

    def _stop(self) -> None:
        self._started = False
        self._current_group = None
        self._current_label = ""


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
    with _global_lock:
        if not tui_enabled():
            # Progress may be disabled while a deployment is running.
            # Discard its display state in that case.
            if _global_progress is not None:
                _global_progress.finish()
                _global_progress = None
            return None
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
        Resource kind.
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
