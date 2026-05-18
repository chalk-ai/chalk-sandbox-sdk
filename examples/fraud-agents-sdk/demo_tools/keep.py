"""Run the investigation but leave the container alive at the end for dashboard
inspection. Stop it later with `python -m demo_tools.stop <id>`.

    uv run python -m demo_tools.keep case_fraud

Also exposed as `--keep` on story.py, which delegates here.

Implementation note: rather than threading a `keep=True` flag through
story.py, we patch `Container.stop` and `ui.container_stopping` for the
duration of this run. story.py stays linear.
"""

import asyncio
import sys
from unittest.mock import patch

from chalkcompute import Container

import story
import ui


def _leave_running(self: Container) -> None:
    ui.container_kept(self)


async def keep(case: str) -> None:
    with patch.object(ui, "container_stopping", lambda: None), \
         patch.object(Container, "stop", _leave_running):
        await story.investigate(case)


def main() -> None:
    asyncio.run(keep(sys.argv[1] if len(sys.argv) > 1 else "case_fraud"))


if __name__ == "__main__":
    main()
