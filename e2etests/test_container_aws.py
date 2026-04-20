"""Trivial Container test exercised against the AWS staging environment.

Sourced credentials live in `.env_aws.enc` (sops-encrypted) and are decrypted
into the environment by the dedicated buildkite pipeline before pytest runs.
"""

import os
import uuid

import pytest
from dotenv import load_dotenv

from chalkcompute import Container, Image

load_dotenv()

CHALK_CLIENT_ID = os.environ.get("CHALK_CLIENT_ID", "")
CHALK_API_SERVER = os.environ.get("CHALK_API_SERVER", "")

needs_aws = pytest.mark.skipif(
    not CHALK_CLIENT_ID or not CHALK_API_SERVER,
    reason="CHALK_CLIENT_ID/CHALK_API_SERVER not set",
)


@needs_aws
def test_container_echo_aws() -> None:
    """A trivial Container.run/.exec roundtrip against the AWS environment."""
    img = Image.debian_slim()
    c = Container(image=img, name=f"aws-trivial-{uuid.uuid4().hex[:8]}")
    try:
        c.run()
        result = c.exec("echo", "hello-aws")
        assert result.exit_code == 0, (
            f"echo exited with code {result.exit_code}: {result.stderr_text}"
        )
        assert result.stdout_text.strip() == "hello-aws"
    finally:
        c.stop()
