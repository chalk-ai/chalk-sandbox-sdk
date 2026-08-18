"""Shared fixtures, markers, and helpers for end-to-end tests.

Every e2e test uses the same OAuth client-credentials flow and the same
Connect-protocol RPC pattern.  This module centralizes:

* ``load_dotenv()`` and the four ``CHALK_*`` env lookups
* the ``needs_staging`` skip marker
* ``_get_access_token`` + ``_connect_rpc`` helpers
* the ``access_token`` / ``auth_headers`` module-scoped fixtures
* the sweep that reaps CI-scoped resources leaked by earlier builds
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import chalkcompute
import httpx
import pytest
from dotenv import load_dotenv

from e2etests._ci_resources import SCOPED_NAME_RE, STALE_AFTER, parse_rfc3339

load_dotenv()

CHALK_CLIENT_ID = os.environ.get("CHALK_CLIENT_ID", "")
CHALK_CLIENT_SECRET = os.environ.get("CHALK_CLIENT_SECRET", "")
CHALK_ENVIRONMENT_ID = os.environ.get("CHALK_ENVIRONMENT_ID", "")
CHALK_API_SERVER = os.environ.get("CHALK_API_SERVER", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"

needs_staging = pytest.mark.skipif(
    not CHALK_CLIENT_ID or not CHALK_API_SERVER,
    reason="CHALK_CLIENT_ID/CHALK_API_SERVER not set",
)

needs_openai = pytest.mark.skipif(
    not OPENAI_API_KEY,
    reason="OPENAI_API_KEY not set",
)


def _deploy_script(
    example_subdir: str,
    script_name: str,
    *,
    function_name: str | None = None,
) -> None:
    """Run an example deploy script as a subprocess.

    Parameters
    ----------
    example_subdir
        Subdirectory under ``examples/`` (e.g. ``"scalar"``).
    script_name
        Script file name within that subdirectory (e.g. ``"deploy_sync.py"``).
    function_name
        Optional function name passed to deploy scripts that support CI isolation.
    """
    script_path = EXAMPLES_DIR / example_subdir / script_name
    env = os.environ.copy()
    if function_name is not None:
        env["CHALK_E2E_FUNCTION_NAME"] = function_name
    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )
    if result.returncode != 0:
        pytest.fail(
            f"{example_subdir}/{script_name} deploy failed "
            f"(exit {result.returncode}):\n"
            f"stdout: {result.stdout[-500:]}\n"
            f"stderr: {result.stderr[-500:]}"
        )


def _delete_function(function_name: str) -> None:
    """Best-effort delete of a deployed function and its scaling group.

    ``DeleteExternalFunctionVersion`` cascades to the backing scaling group, so
    deleting the function version is all a test needs to undo a deploy.  Errors
    are swallowed: a cleanup failure should never fail an otherwise green test.
    """
    try:
        chalkcompute.RemoteFunction.from_name(function_name).delete()
    except Exception as exc:
        print(f"warning: could not delete function {function_name}: {exc}")


def _get_access_token() -> str:
    """Exchange client credentials for a bearer token via the Chalk REST API."""
    resp = httpx.post(
        f"{CHALK_API_SERVER}/v1/oauth/token",
        json={
            "client_id": CHALK_CLIENT_ID,
            "client_secret": CHALK_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _connect_rpc(
    service: str,
    method: str,
    body: dict,
    headers: dict[str, str],
    timeout: float = 30,
) -> httpx.Response:
    """Make a Connect-protocol unary RPC call.

    Parameters
    ----------
    service
        Fully-qualified service name (e.g. ``"chalk.container.v1.ContainerService"``).
    method
        RPC method name on that service.
    """
    return httpx.post(
        f"{CHALK_API_SERVER}/{service}/{method}",
        json=body,
        headers=headers,
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def access_token() -> str:
    return _get_access_token()


@pytest.fixture(scope="module")
def auth_headers(access_token: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "X-Chalk-Env-Id": CHALK_ENVIRONMENT_ID,
    }


# ---------------------------------------------------------------------------
# Stale CI resource sweep
# ---------------------------------------------------------------------------

_SG_SERVICE = "chalk.scalinggroup.v1.ScalingGroupManagerService"

_LIST_PAGE_SIZE = 100
_LIST_PAGE_CAP = 50


def _iter_scaling_groups(headers: dict[str, str]) -> Iterator[dict]:
    """Yield every scaling group in the environment, following the cursor."""
    cursor = ""
    for _ in range(_LIST_PAGE_CAP):
        body: dict[str, object] = {"limit": _LIST_PAGE_SIZE}
        if cursor:
            body["cursor"] = cursor
        resp = _connect_rpc(_SG_SERVICE, "ListScalingGroups", body, headers)
        resp.raise_for_status()
        page = resp.json()
        yield from page.get("scalingGroups", [])
        cursor = page.get("nextCursor") or ""
        if not cursor:
            return


def _sweep_stale_scoped_scaling_groups() -> None:
    """Delete build-scoped scaling groups left behind by earlier CI builds.

    ``teardown_class`` covers the happy path, but it never runs when a Buildkite
    job times out, is cancelled, or loses its agent.  Without this net every such
    build leaks a scaling group -- and its running replicas -- indefinitely.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_get_access_token()}",
        "X-Chalk-Env-Id": CHALK_ENVIRONMENT_ID,
    }
    now = datetime.now(timezone.utc)
    for sg in _iter_scaling_groups(headers):
        name = sg.get("name", "")
        if sg.get("deletedAt") or not SCOPED_NAME_RE.match(name):
            continue
        created_at = parse_rfc3339(sg.get("createdAt"))
        if created_at is None or now - created_at < STALE_AFTER:
            continue
        resp = _connect_rpc(
            _SG_SERVICE, "DeleteScalingGroup", {"id": sg["id"]}, headers
        )
        outcome = "deleted" if resp.is_success else f"failed ({resp.status_code})"
        print(f"stale scaling group {name} ({created_at.isoformat()}): {outcome}")


@pytest.fixture(scope="session", autouse=True)
def sweep_stale_ci_resources() -> None:
    """Reap scoped resources leaked by earlier builds before any test runs."""
    if not CHALK_CLIENT_ID or not CHALK_API_SERVER:
        return
    try:
        _sweep_stale_scoped_scaling_groups()
    except Exception as exc:
        print(f"warning: stale resource sweep skipped: {exc}")
