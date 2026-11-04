"""End-to-end coverage for sandbox metadata after termination."""

import pytest

from chalkcompute import SandboxClient
from e2etests.conftest import needs_staging


@needs_staging
@pytest.mark.parametrize("attached", [False, True], ids=["created", "attached"])
def test_sandbox_metadata_after_termination(attached: bool) -> None:
    with SandboxClient.from_env() as client:
        sandbox = client.create("ubuntu:latest")
        try:
            sandbox_id = sandbox.id
            assert sandbox_id is not None
            if attached:
                sandbox = client.get(sandbox_id)
                sandbox.refresh()

            sandbox.terminate(grace_period_seconds=30)
            info = sandbox.info
            assert info is not None
            assert info.id == sandbox_id
            assert info.status == "terminated"
            assert info.status_message == "Termination requested"
            assert sandbox.refresh() == info
            sandbox.terminate()
        finally:
            sandbox.terminate()
