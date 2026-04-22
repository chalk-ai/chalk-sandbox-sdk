"""Shared utilities for example tests and deploys.

Provides:
- Unique function naming per CI run to support parallel execution
- Short hash generation from git commit or CI env vars
"""

import hashlib
import os
import subprocess
from functools import lru_cache


@lru_cache(maxsize=1)
def get_test_run_hash() -> str:
    """Get a short hash for this test run.

    Uses (in order):
    1. CHALK_TEST_RUN_HASH env var (for explicit control in CI)
    2. First 8 chars of git commit SHA
    3. Random 8-char hash

    Returns
    -------
    str
        8-character hex hash.
    """
    # Explicit override from CI
    if hash_env := os.environ.get("CHALK_TEST_RUN_HASH"):
        return hash_env[:8]

    # Try git commit SHA
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()[:8]
    except Exception:
        pass

    # Fallback to random hash based on timestamp + pid
    import time
    import random
    data = f"{time.time()}{os.getpid()}{random.random()}".encode()
    return hashlib.md5(data).hexdigest()[:8]


def make_function_name(base_name: str) -> str:
    """Create a unique function name for this test run.

    Parameters
    ----------
    base_name
        Base function name (e.g., "add", "count-up").

    Returns
    -------
    str
        Function name with hash (e.g., "chalk-sandbox-test-a1b2c3d4-add").
    """
    hash_val = get_test_run_hash()
    return f"chalk-sandbox-test-{hash_val}-{base_name}"
