"""Deploy scalar functions that exercise nested gather/defer calls.

CHALK_FUNCTION_STRIP_SOURCE=1 uv run python examples/scalar/deploy_nested_gather.py
"""

from __future__ import annotations

import logging

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


@chalkcompute.function(
    image=chalkcompute.Image.debian_slim().pip_install(["chalkcompute>=2.1.3"]),
)
def nested_gather_add(values: list[int]) -> list[int]:
    """Fan out deferred calls from inside a remote function and gather them."""
    from datetime import datetime, timezone

    import chalkcompute

    add = chalkcompute.RemoteFunction.from_name("add")
    return chalkcompute.gather(
        [
            add.with_knowledge_cutoff(datetime.now(timezone.utc)).defer(1, value)
            for value in values
        ]
    )
