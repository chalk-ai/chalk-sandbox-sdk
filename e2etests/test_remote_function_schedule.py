"""End-to-end coverage for scheduled remote function registration."""

from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from dotenv import load_dotenv

import chalkcompute
from chalkcompute._function import _CATALOG_SVC

load_dotenv()

needs_chalk_auth = pytest.mark.skipif(
    not os.environ.get("CHALK_API_SERVER") or not os.environ.get("CHALK_CLIENT_ID"),
    reason="requires CHALK_API_SERVER and CHALK_CLIENT_ID",
)


def _load_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


def _delete_version(client: Any, version_id: str) -> None:
    client.rpc(_CATALOG_SVC, "DeleteExternalFunctionVersion", {"id": version_id})


def _register_unscheduled_latest_version(fn: chalkcompute.RemoteFunction) -> str | None:
    """Register a no-schedule latest version so the test schedule is removed."""
    if fn.version_info is None:
        return None

    client = fn._client
    get_resp = client.rpc(
        _CATALOG_SVC,
        "GetExternalFunctionVersion",
        {"id": fn.version_info.id, "includeScalingGroup": True},
    )
    if get_resp.status_code != 200:
        return None

    data = get_resp.json()
    efv = data["externalFunctionVersion"]
    scaling_group = data.get("scalingGroup")
    if not scaling_group or "spec" not in scaling_group:
        return None

    create_resp = client.rpc(
        _CATALOG_SVC,
        "CreateExternalFunctionVersion",
        {
            "functionName": efv["functionName"],
            "inputArrowSchema": efv.get("inputArrowSchema", {"columns": []}),
            "outputArrowSchema": efv.get("outputArrowSchema", {"columns": []}),
            "spec": scaling_group["spec"],
        },
        timeout=60,
    )
    if create_resp.status_code != 200:
        return None

    return create_resp.json()["externalFunctionVersion"]["id"]


@needs_chalk_auth
def test_remote_function_registers_with_schedule(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CHALK_FUNCTION_STRIP_SOURCE", "1")

    module_path = tmp_path / "scheduled_fn.py"
    module_path.write_text(
        "def scheduled_tick() -> None:\n"
        "    return None\n",
    )
    module = _load_module(module_path)

    name = f"chalk-sandbox-test-scheduled-{uuid.uuid4().hex[:8]}"
    fn = chalkcompute.RemoteFunction(
        module.scheduled_tick,
        name=name,
        schedule="1h",
        min_replicas=1,
        max_replicas=1,
    )

    unscheduled_version_id: str | None = None
    try:
        fn.deploy(build_timeout=600, ready_timeout=300)
        assert fn.version_info is not None

        resp = fn._client.rpc(
            _CATALOG_SVC,
            "GetExternalFunctionVersion",
            {"id": fn.version_info.id},
        )
        assert resp.status_code == 200, f"GetExternalFunctionVersion failed: {resp.text}"

        config = resp.json()["externalFunctionVersion"].get("config") or {}
        assert config.get("schedule") == "1h"
    finally:
        unscheduled_version_id = _register_unscheduled_latest_version(fn)
        if fn.version_info is not None:
            _delete_version(fn._client, fn.version_info.id)
            fn._version_info = None
        if unscheduled_version_id is not None:
            _delete_version(fn._client, unscheduled_version_id)
