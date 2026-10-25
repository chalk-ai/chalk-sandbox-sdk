#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute>=2.11.1"]
# ///

"""Deploy a scalar function that exercises nested gather/defer calls.

The deployment intentionally persists for subsequent calls and depends on the
persistent ``ex-add`` deployment from ``deploy_sync.py``.
"""

from __future__ import annotations

import logging
import os

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


@chalkcompute.function(
    name=os.environ.get("CHALK_E2E_FUNCTION_NAME", "ex-nested-gather-add"),
    image=chalkcompute.Image.debian_slim("3.11").pip_install(
        ["chalkcompute>=2.11.1", "connectrpc>=0.12.1,<0.13.0"]
    ),
)
def nested_gather_add(values: list[int]) -> list[int]:
    """Fan out deferred calls from inside a remote function and gather them."""
    from datetime import datetime, timezone

    import chalkcompute

    add = chalkcompute.RemoteFunction.from_name("ex-add")
    return chalkcompute.gather(
        [
            add.with_knowledge_cutoff(datetime.now(timezone.utc)).defer(1, value)
            for value in values
        ]
    )
