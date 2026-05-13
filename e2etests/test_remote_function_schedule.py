"""End-to-end coverage for scheduled remote function registration."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

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


@needs_chalk_auth
def test_remote_function_registers_with_schedule(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CHALK_FUNCTION_STRIP_SOURCE", "1")

    module_path = tmp_path / "scheduled_fn.py"
    module_path.write_text(
        "def scheduled_tick() -> None:\n    return None\n",
    )
    module = _load_module(module_path)

    name = "chalk-sandbox-test-scheduled"
    fn = chalkcompute.RemoteFunction(
        module.scheduled_tick,
        name=name,
        schedule="1m",
        min_replicas=1,
        max_replicas=1,
    )

    fn.deploy(build_timeout=600, ready_timeout=300)
    assert fn.version_info is not None

    resp = fn._client.rpc(
        _CATALOG_SVC,
        "GetExternalFunctionVersion",
        {"id": fn.version_info.id},
    )
    assert resp.status_code == 200, f"GetExternalFunctionVersion failed: {resp.text}"

    config = resp.json()["externalFunctionVersion"].get("config") or {}
    assert config.get("schedule") == "1m"

    fn = chalkcompute.RemoteFunction.from_name("scheduled_tick")
    res = fn()
    assert res is None
