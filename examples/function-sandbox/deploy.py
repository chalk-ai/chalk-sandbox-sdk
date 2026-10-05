#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["chalkcompute>=2.11.1"]
# ///

# # Create a sandbox from a deployed function
#
# A deployed function can use its workload identity to create a nested sandbox
# without copying local Chalk credentials into the function image. The function
# starts a minimal Python sandbox, executes one command, and always terminates it.
# The outer function deployment intentionally persists so callers can inspect
# this behavior; only the nested sandbox is ephemeral.

from __future__ import annotations

import os

import chalkcompute

SANDBOX_E2E_IMAGE = os.environ.get("CHALK_SANDBOX_E2E_IMAGE")


@chalkcompute.function(
    name=os.environ.get("CHALK_E2E_FUNCTION_NAME", "ex-function-sandbox"),
    image=chalkcompute.Image.debian_slim("3.12").pip_install(["chalkcompute>=2.11.1"]),
    env={"CHALK_SANDBOX_E2E_IMAGE": SANDBOX_E2E_IMAGE} if SANDBOX_E2E_IMAGE else None,
)
def create_sandbox(command: list[str] | None = None) -> str:
    """Run a command in a nested sandbox, defaulting to a simple echo."""
    import os

    from chalkcompute import Image, Sandbox

    if command is None:
        command = ["echo", "sandbox-exec-ok"]

    sandbox = Sandbox(
        image=os.environ.get("CHALK_SANDBOX_E2E_IMAGE") or Image.debian_slim("3.12")
    )
    try:
        sandbox.run()
        result = sandbox.exec(*command)
        if result.exit_code != 0:
            raise RuntimeError(
                "Sandbox exec failed: "
                f"exit_code={result.exit_code}, stderr={result.stderr_text}"
            )
        return result.stdout_text
    finally:
        if sandbox.id is not None:
            sandbox.terminate()
