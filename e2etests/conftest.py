"""Shared fixtures, markers, and helpers for end-to-end tests.

Every e2e test uses the same OAuth client-credentials flow and the same
Connect-protocol RPC pattern.  This module centralizes:

* ``load_dotenv()`` and the four ``CHALK_*`` env lookups
* the ``needs_staging`` skip marker
* ``_get_access_token`` + ``_connect_rpc`` helpers
* the ``access_token`` / ``auth_headers`` module-scoped fixtures
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from dotenv import load_dotenv

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


def _deploy_script(example_subdir: str, script_name: str) -> None:
    """Run an example deploy script as a subprocess.

    Parameters
    ----------
    example_subdir
        Subdirectory under ``examples/`` (e.g. ``"scalar"``).
    script_name
        Script file name within that subdirectory (e.g. ``"deploy_sync.py"``).
    """
    script_path = EXAMPLES_DIR / example_subdir / script_name
    env = os.environ.copy()
    env["PYTHONPATH"] = (
        f"{REPO_ROOT}{os.pathsep}{env['PYTHONPATH']}"
        if env.get("PYTHONPATH")
        else str(REPO_ROOT)
    )
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
        Fully-qualified service name (e.g. ``"chalk.volume.v1.VolumeService"``).
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
