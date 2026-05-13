"""End-to-end coverage for scheduled remote function registration."""

from __future__ import annotations

import os
import uuid

import chalkcompute
from chalkcompute._function import _CATALOG_SVC
from e2etests.conftest import _deploy_script, needs_staging

RUN_ID = uuid.uuid4().hex[:8]


@needs_staging
def test_remote_function_registers_with_schedule() -> None:
    """Deploy the scheduled heartbeat example, verify the schedule is persisted
    in the catalog, and exercise the no-input invoke path."""
    function_name = f"test_heartbeat_{RUN_ID}"
    os.environ["CHALK_HEARTBEAT_FUNCTION_NAME"] = function_name
    _deploy_script("scheduled-function", "heartbeat.py")

    fn = chalkcompute.RemoteFunction.from_name(function_name)
    assert fn.version_info is not None

    resp = fn._client.rpc(
        _CATALOG_SVC,
        "GetExternalFunctionVersion",
        {"id": fn.version_info.id},
    )
    assert resp.status_code == 200, f"GetExternalFunctionVersion failed: {resp.text}"

    config = resp.json()["externalFunctionVersion"].get("config") or {}
    assert config.get("schedule") == "10m"

    assert fn() is None
