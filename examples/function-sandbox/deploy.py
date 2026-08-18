"""Deploy a function that creates a sandbox with its pod identity.

uv run python examples/function-sandbox/deploy.py
"""

from __future__ import annotations

import os

import chalkcompute

FUNCTION_NAME = os.environ.get("CHALK_E2E_FUNCTION_NAME", "ex-function-sandbox")


@chalkcompute.function(
    name=FUNCTION_NAME,
    image=chalkcompute.Image.debian_slim("3.11").pip_install(["chalkcompute>=2.9.4"]),
    env={"CHALK_E2E_SANDBOX_NAME": FUNCTION_NAME},
)
def create_sandbox() -> str:
    """Create and terminate a sandbox using the deployed function's credentials."""
    import os

    from chalkcompute import SandboxClient

    sandbox_image = os.environ.get(
        "CHALK_SANDBOX_E2E_IMAGE",
        "us-central1-docker.pkg.dev/chalk-develop/base-images/chalk-sandbox-entrypoint",
    )
    with SandboxClient.from_env() as client:
        sandbox = client.create(
            sandbox_image,
            name=os.environ["CHALK_E2E_SANDBOX_NAME"],
        )
        try:
            return sandbox.id
        finally:
            sandbox.terminate()
