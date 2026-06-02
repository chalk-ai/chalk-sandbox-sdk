"""End-to-end coverage for scheduled remote function registration."""

from __future__ import annotations

import chalkcompute
from chalkcompute._function import _CATALOG_SVC
from e2etests.conftest import _deploy_script, needs_staging


@needs_staging
def test_remote_function_registers_with_schedule() -> None:
    """Deploy the scheduled heartbeat example, verify the schedule is persisted
    in the catalog, and exercise the no-input invoke path."""
    _deploy_script("scheduled-function", "heartbeat.py")

    fn = chalkcompute.RemoteFunction.from_name("heartbeat")
    assert fn.version_info is not None

    resp = fn._client.rpc(
        _CATALOG_SVC,
        "GetExternalFunctionVersion",
        {"id": fn.version_info.id},
    )
    assert resp.status_code == 200, f"GetExternalFunctionVersion failed: {resp.text}"

    config = resp.json()["externalFunctionVersion"].get("config") or {}
    assert config.get("schedule") == "10m"

    assert fn.remote() is None
