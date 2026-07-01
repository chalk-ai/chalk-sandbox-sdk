"""Smoke test: create -> call -> delete a remote function.

Run from the repo root so it uses the editable chalkcompute:
    uv run python examples/try_remote_fn.py

Auth comes from ~/.chalk.yml `default` token (http://localhost:4002, env avi-chalk-ai)
since no CHALK_* env vars are set. Make sure the local API server is up on :4002.
"""

from __future__ import annotations

import logging

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


# --- CREATE ---------------------------------------------------------------
# The decorator deploys the function at import time (registers a version and
# builds the deploy image server-side). `add` becomes a RemoteFunction handle.
@chalkcompute.function(name="avi-try-add")
def add(a: int, b: int) -> int:
    return a + b


def main() -> None:
    # --- CALL (blocking) --------------------------------------------------
    print("remote(1, 10) =", add.remote(1, 10))

    # --- CALL (deferred) --------------------------------------------------
    handle = add.defer(2, 20)
    print("defer(2, 20).get() =", handle.get(timeout=60))

    # You can also grab a handle by name without redeploying:
    same = chalkcompute.RemoteFunction.from_name("avi-try-add")
    print("from_name().remote(3, 30) =", same.remote(3, 30))

    # --- DELETE -----------------------------------------------------------
    add.delete()
    print("deleted avi-try-add")


if __name__ == "__main__":
    main()
