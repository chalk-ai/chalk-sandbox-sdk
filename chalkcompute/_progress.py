"""Deployment status with live terminal updates and append-only fallback output."""

from __future__ import annotations

import atexit
import os
import re
import sys
import threading
from typing import Literal

from chalkcompute._progress_terminal import LiveDisplay

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
    return mode in ("auto", "true", "1", "plain")


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
    """Thread-safe deployment tracker shared by all concurrent operations.

    Live output is event-driven, with no refresh thread or alternate screen.
    Captured output prints only changed steps, each with its resource identity.
    """

    def __init__(self) -> None:
        self._resources: dict[str, _ResourceState] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._started = False
        self._display: LiveDisplay | None = None
        self._presentation_selected = False

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

    @staticmethod
    def _text(value: str) -> str:
        # Status labels can contain server messages or user-supplied names.
        # Never let embedded terminal controls move the cursor or style logs.
        value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
        return "".join(c for c in " ".join(value.split()) if c.isprintable())

    def _row(self, state: _ResourceState, *, root: bool) -> str:
        detail = self._text(state.detail)
        if not root:
            label = self._text(state.label)
            return f"{label} — {detail}" if detail else label
        if state.status in _TERMINAL_STATUSES:
            label = {"done": "Done", "failed": "Failed", "skipped": "Skipped"}[
                state.status
            ]
            if state.status == "done" and detail in ("ready", "running"):
                return detail.capitalize()
            return f"{label} — {detail}" if detail else label
        return detail

    def _refresh(self) -> None:
        stream = self._display.stream if self._display else sys.stderr
        plain = os.environ.get("CHALK_SANDBOX_TUI", "").strip().lower() == "plain"
        color = supports_color(stream) and not plain
        lines: list[str] = []
        live_lines: list[str] = []
        changed = False
        for key in self._order:
            root = self._resources[key]
            root_label = self._text(root.label)
            root_changed = (
                root.last_rendered is not None and root.last_rendered[1] != root.label
            )
            pending = [(root, 0, root_label)]
            root_lines: list[str] = []
            root_changes: list[str] = []
            final_line: str | None = None
            while pending:
                state, depth, path = pending.pop()
                pending.extend(
                    (
                        child,
                        depth + 1,
                        path
                        if state is root
                        else f"{path} / {self._text(state.label)}",
                    )
                    for child in reversed(state.children)
                )
                snapshot = (state.status, state.label, state.detail, depth)
                row_changed = snapshot != state.last_rendered or root_changed
                state.last_rendered = snapshot
                row = self._row(state, root=state is root)
                if not row:
                    continue  # A resource declaration alone is not progress.
                mark, fallback, code = _STATUS_MARKS[state.status]
                if encodable(mark, stream) != mark:
                    mark = fallback
                marker = styled(mark, code, color=color)
                identity = styled(path, "1", color=color)
                line = encodable(f"{marker} {identity}: {row}", stream)
                if row_changed:
                    if state is root and state.status in _TERMINAL_STATUSES:
                        final_line = line
                    else:
                        root_changes.append(line)
                    changed = True
                if state is root:
                    root_lines.append(
                        encodable(
                            f"{marker} {styled(root_label, '1', color=color)}: {row}",
                            stream,
                        )
                    )
                elif root.status != "done":
                    # Successful operations collapse to their summary. Keep
                    # step details while running and when diagnosing failures.
                    root_lines.append(
                        encodable(f"{'  ' * depth}{marker} {row}", stream)
                    )
            if root_lines:
                if not self._row(root, root=True):
                    root_lines.insert(
                        0, encodable(styled(root_label, "1", color=color), stream)
                    )
                live_lines.extend(root_lines)
            lines.extend(root_changes)
            if final_line is not None:
                lines.append(final_line)

        if not changed:
            return
        try:
            if self._display is not None and (
                plain or not self._display.owns_streams()
            ):
                self._display.close()
                self._display = None
                stream = sys.stderr
                # Re-render this change against the new stream/encoding.
                for state in self._resources.values():
                    state.last_rendered = None
                self._refresh()
                return
            if not self._presentation_selected:
                self._presentation_selected = True
                if not plain:
                    self._display = LiveDisplay.create(stream, self._lock)
            if self._display is not None:
                self._display.update(live_lines)
            elif lines:
                stream.write("\n".join(lines) + "\n")
                stream.flush()
        except (OSError, ValueError):
            # A broken pipe or closed console must not fail a deployment.
            self._close_display()

    def _start(self) -> None:
        self._started = True

    def _close_display(self) -> None:
        display, self._display = self._display, None
        if display is not None:
            try:
                display.close()
            except (OSError, ValueError):
                pass

    def _stop(self) -> None:
        self._close_display()
        self._started = False
        self._presentation_selected = False


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


# Restore terminal state even when a script exits during a deployment.
atexit.register(reset_progress)
