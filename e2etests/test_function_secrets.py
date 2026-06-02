"""E2E test: Secret.from_local_env() with @chalkcompute.function."""

from __future__ import annotations

import logging
import os
import time
import uuid

import pytest
from dotenv import load_dotenv

import chalkcompute
from chalkcompute import Image, Secret
from e2etests._fn_read_secret import read_secret

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

CHALK_CLIENT_ID = os.environ.get("CHALK_CLIENT_ID", "")
CHALK_API_SERVER = os.environ.get("CHALK_API_SERVER", "")

needs_staging = pytest.mark.skipif(
    not CHALK_CLIENT_ID or not CHALK_API_SERVER,
    reason="CHALK_CLIENT_ID/CHALK_API_SERVER not set",
)


def _call_with_retry(
    fn: chalkcompute.RemoteFunction,
    *args: object,
    retries: int = 5,
    delay: float = 5.0,
) -> object:
    for attempt in range(retries):
        try:
            return fn.remote(*args)
        except RuntimeError as e:
            if "Unavailable" in str(e) and attempt < retries - 1:
                logging.info("Call attempt %d failed (Unavailable), retrying in %.0fs...", attempt + 1, delay)
                time.sleep(delay)
            else:
                raise
    raise AssertionError("unreachable")


@needs_staging
def test_function_secret_from_local_env() -> None:
    """Secret.from_local_env() injects a local env var into a deployed function."""
    local_var = f"TEST_FN_SECRET_{uuid.uuid4().hex[:8].upper()}"
    secret_value = f"fn-secret-{uuid.uuid4().hex[:8]}"
    os.environ[local_var] = secret_value

    fn: chalkcompute.RemoteFunction | None = None
    try:
        fn = chalkcompute.RemoteFunction(
            read_secret,
            image=Image.debian_slim(),
            name=f"test-fn-secret-{uuid.uuid4().hex[:8]}",
            secrets=[Secret.from_local_env(local_var)],
        )
        fn.deploy()
        fn.wait_ready()

        result = _call_with_retry(fn, local_var)
        assert result == secret_value, (
            f"Expected {secret_value!r}, got {result!r}"
        )
    finally:
        del os.environ[local_var]
        if fn is not None:
            fn.delete()
