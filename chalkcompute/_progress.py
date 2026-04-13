"""
Rich hierarchical spinner display for Container.run() steps.

Activated when CHALK_COMPUTE_SPINNERS=1 is set in the environment.
Falls back to plain print() when disabled or when rich is not installed.
"""

from __future__ import annotations

import logging
import os
import types
from typing import TYPE_CHECKING, Literal, Union

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from rich.console import ConsoleRenderable, RichCast
    from rich.live import Live

    RenderableType = Union[ConsoleRenderable, RichCast, str]

StepName = Literal[
    "volume",
    "upload",
    "image_check",
    "image_build",
    "boot",
    "files_sync",
]

StepStatus = Literal["pending", "in_progress", "done", "failed", "skipped"]

_STEP_LABELS: dict[StepName, str] = {
    "volume": "Creating volume",
    "upload": "Uploading files",
    "image_check": "Checking image",
    "image_build": "Building image",
    "boot": "Booting container",
    "files_sync": "Syncing files",
}


def spinners_enabled() -> bool:
    return os.environ.get("CHALK_COMPUTE_SPINNERS") == "1"


class ContainerProgress:
    """Hierarchical spinner display for container launch steps.

    Usage::

        with ContainerProgress("my-container", ["volume", "upload", "boot"]) as p:
            p.update("volume", "in_progress")
            ...
            p.update("volume", "done")
    """

    def __init__(self, container_name: str, steps: list[StepName]) -> None:
        self._name = container_name
        self._steps = steps
        self._status: dict[StepName, StepStatus] = {s: "pending" for s in steps}
        self._detail: dict[StepName, str] = {}
        self._live: Live | None = None
        self._use_rich = False

    def __enter__(self) -> ContainerProgress:
        if not spinners_enabled():
            return self
        try:
            from rich.live import Live

            self._use_rich = True
            self._live = Live(
                self._render(),
                refresh_per_second=12,
                transient=False,
            )
            self._live.__enter__()
        except ImportError:
            pass
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        if self._live is not None:
            self._live.update(self._render())
            self._live.__exit__(exc_type, exc_val, exc_tb)

    def update(
        self,
        step: StepName,
        status: StepStatus,
        detail: str = "",
    ) -> None:
        self._status[step] = status
        if detail:
            self._detail[step] = detail

        if self._live is not None:
            self._live.update(self._render())
        elif not self._use_rich:
            self._print_update(step, status, detail)

    def _print_update(self, step: StepName, status: StepStatus, detail: str) -> None:
        """Fallback plain-text output when rich is not active."""
        label = _STEP_LABELS.get(step, step)
        if detail:
            label = f"{label} ({detail})"
        if status == "in_progress":
            logger.info("  ⠸ %s...", label)
        elif status == "done":
            logger.info("  ✓ %s", label)
        elif status == "failed":
            logger.info("  ✗ %s", label)

    def _render(self):
        """Build a rich renderable showing the hierarchical step tree."""
        from rich.spinner import Spinner
        from rich.table import Table
        from rich.text import Text

        all_done = all(s in ("done", "skipped") for s in self._status.values())
        any_failed = any(s == "failed" for s in self._status.values())

        grid = Table.grid(padding=(0, 0))
        grid.add_column(width=4, justify="right")
        grid.add_column()

        # Header row
        if any_failed:
            header_icon = Text(" ✗ ", style="bold red")
        elif all_done:
            header_icon = Text(" ✓ ", style="bold green")
        else:
            header_icon = Spinner("dots", style="bold cyan")

        grid.add_row(header_icon, Text(f" Container \"{self._name}\"", style="bold"))

        # Step rows
        for step in self._steps:
            status = self._status[step]
            label = _STEP_LABELS.get(step, step)
            detail = self._detail.get(step, "")
            if detail:
                label = f"{label} — {detail}"

            if status == "in_progress":
                icon = Spinner("dots", style="cyan")
            elif status == "done":
                icon = Text("✓", style="green")
            elif status == "failed":
                icon = Text("✗", style="red")
            elif status == "skipped":
                icon = Text("–", style="dim")
            else:
                icon = Text("◌", style="dim")

            label_style = "dim" if status == "pending" else ""
            grid.add_row(Text("   "), _icon_label(icon, label, label_style))

        return grid


def _icon_label(icon: RenderableType, label: str, style: str = ""):
    """Combine a spinner or text icon with a label in a tight grid."""
    from rich.table import Table
    from rich.text import Text

    row = Table.grid(padding=(0, 1))
    row.add_column(width=2)
    row.add_column()
    row.add_row(icon, Text(label, style=style))
    return row
