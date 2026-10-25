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
# starts a sandbox, runs a command, and always terminates it. The command
# defaults to ``echo sandbox-exec-ok`` and can be changed at deployment.
# The outer function deployment intentionally persists so callers can inspect
# this behavior; only the nested sandbox is ephemeral.

from __future__ import annotations

import os

import chalkcompute

SANDBOX_E2E_ENV = {
    name: os.environ[name]
    for name in ("CHALK_SANDBOX_E2E_IMAGE", "CHALK_SANDBOX_E2E_COMMAND")
    if os.environ.get(name)
}


@chalkcompute.function(
    name=os.environ.get("CHALK_E2E_FUNCTION_NAME", "ex-function-sandbox"),
    image=chalkcompute.Image.debian_slim(
        os.environ.get("CHALK_E2E_PYTHON_VERSION", "3.12")
    ),
    env=SANDBOX_E2E_ENV or None,
)
def create_sandbox() -> str:
    """Run a command in a nested sandbox using the pod's credentials."""
    import json
    import os

    from chalkcompute import Image, Sandbox

    sandbox = Sandbox(
        image=os.environ.get("CHALK_SANDBOX_E2E_IMAGE") or Image.debian_slim("3.12")
    )
    command = (
        json.loads(os.environ["CHALK_SANDBOX_E2E_COMMAND"])
        if os.environ.get("CHALK_SANDBOX_E2E_COMMAND")
        else ["echo", "sandbox-exec-ok"]
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
