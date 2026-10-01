"""Naming and staleness rules for CI-scoped e2e resources.

Deliberately free of import-time side effects -- no ``load_dotenv()``, no
credential lookups -- so unit tests can import these rules without dragging the
e2e environment into ``os.environ``.  The RPC-driven sweep that consumes them
lives in ``conftest.py``.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timedelta

# Only clean up names with a CI-generated suffix,
# like: ex-<resource-name>-<8 or 12 hexadecimal characters>
SCOPED_NAME_RE = re.compile(r"^ex-[a-z0-9-]+-(?:[0-9a-f]{8}|[0-9a-f]{12})$")

# These four e2e groups belong to model deployments. They must be deleted via
# ModelDeploymentService, not the standalone scaling-group API.
MODEL_SCALING_GROUP_NAME_RE = re.compile(
    r"^ex-(?:trivial-double|volume-triple|handler-affine|combined-affine)-sg-"
    r"(?:[0-9a-f]{8}|[0-9a-f]{12})$"
)

# The longest integration-test step allows 30 minutes and two automatic retries,
# so nothing older than this can belong to a build that is still running.
STALE_AFTER = timedelta(hours=3)


def build_scoped_name(base: str) -> str:
    """Return a stable resource name isolated to the current CI job.

    Scoped to the *job* rather than the build because a job retry reuses its
    build id: with build scoping, a retry redeploys the exact name the previous
    attempt's teardown just deleted, and the server can then resolve that name
    to the scaling group it no longer has.  Job ids differ per attempt, so every
    retry starts from a name that has never existed -- and an attempt that died
    before teardown leaves an orphan for the sweep instead of a name the retry
    silently reuses.

    ``BUILDKITE_BUILD_ID`` remains a fallback; a local run with neither set gets
    the bare name, which the sweep never matches.
    """
    run_id = (
        os.environ.get("BUILDKITE_JOB_ID") or os.environ.get("BUILDKITE_BUILD_ID") or ""
    ).replace("-", "")
    if not run_id:
        return base
    # Hash the full run ID and take the first 12 characters.
    suffix = hashlib.sha256(run_id.encode()).hexdigest()[:12]
    return f"{base}-{suffix}"


def parse_rfc3339(value: str | None) -> datetime | None:
    """Parse a proto3 JSON timestamp, tolerating nanosecond precision."""
    if not value:
        return None
    normalized = re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None
