"""Naming and staleness rules for CI-scoped e2e resources.

Deliberately free of import-time side effects -- no ``load_dotenv()``, no
credential lookups -- so unit tests can import these rules without dragging the
e2e environment into ``os.environ``.  The RPC-driven sweep that consumes them
lives in ``conftest.py``.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta

# Names built by ``build_scoped_name`` end in the first eight hex characters of a
# Buildkite build id.  Fixed example names (``ex-add``, ``ex-slow-add``, ...) and
# local runs -- which have no build id, so no suffix -- deliberately do not match,
# so the sweep can only ever delete resources CI created.
SCOPED_NAME_RE = re.compile(r"^ex-[a-z0-9-]+-[0-9a-f]{8}$")

# The longest integration-test step allows 30 minutes and two automatic retries,
# so nothing older than this can belong to a build that is still running.
STALE_AFTER = timedelta(hours=3)


def build_scoped_name(base: str) -> str:
    """Return a stable resource name isolated to the current CI build."""
    build_id = os.environ.get("BUILDKITE_BUILD_ID", "").replace("-", "")
    if not build_id:
        return base
    return f"{base}-{build_id[:8]}"


def parse_rfc3339(value: str | None) -> datetime | None:
    """Parse a proto3 JSON timestamp, tolerating nanosecond precision."""
    if not value:
        return None
    normalized = re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None
