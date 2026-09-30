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
from typing import Any

import chalkcompute
import pytest
from pyqwest import FullResponse, SyncClient
from dotenv import load_dotenv

from e2etests._ci_resources import SCOPED_NAME_RE, STALE_AFTER, parse_rfc3339

load_dotenv()

CHALK_CLIENT_ID = os.environ.get("CHALK_CLIENT_ID", "")
CHALK_CLIENT_SECRET = os.environ.get("CHALK_CLIENT_SECRET", "")
_HTTP_CLIENT = SyncClient()
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

needs_function_sandbox_identity = pytest.mark.skipif(
    os.environ.get("CHALK_RUN_FUNCTION_SANDBOX_E2E") != "1",
    reason=(
        "CHALK_RUN_FUNCTION_SANDBOX_E2E=1 not set; the target environment must "
        "grant deployed functions deploy.redeploy and sandbox image-pull access"
    ),
)


def _deploy_script(
    example_subdir: str,
    script_name: str,
    *,
    function_name: str | None = None,
    extra_env: dict[str, str] | None = None,
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
    extra_env
        Additional environment variables passed to the deploy script.
    """
    script_path = EXAMPLES_DIR / example_subdir / script_name
    env = os.environ.copy()
    if function_name is not None:
        env["CHALK_E2E_FUNCTION_NAME"] = function_name
    if extra_env is not None:
        env.update(extra_env)
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


def _delete_model_registration(model_name: str, model_version: int | None) -> None:
    """Best-effort delete of an e2e model version and its model registration."""
    from protobuf import Oneof

    from chalkcompute._connect import ConnectClient
    from chalkcompute._gen.chalk.server.v1 import (
        model_registry_connect,
        model_registry_pb,
    )

    client = ConnectClient()
    try:
        stub = client.stub(model_registry_connect.ModelRegistryServiceClientSync)
        if model_version is not None:
            try:
                client.call(
                    stub.delete_model_version,
                    model_registry_pb.DeleteModelVersionRequest(
                        model_version_key=model_registry_pb.ModelVersionKey(
                            model_name=model_name,
                            version=model_version,
                        )
                    ),
                )
            except Exception as exc:
                print(
                    f"warning: could not delete model version "
                    f"{model_name} v{model_version}: {exc}"
                )
        try:
            client.call(
                stub.delete_model,
                model_registry_pb.DeleteModelRequest(
                    model_identifier=Oneof("model_name", model_name)
                ),
            )
        except Exception as exc:
            print(f"warning: could not delete model {model_name}: {exc}")
    except Exception as exc:
        print(f"warning: could not initialize model cleanup for {model_name}: {exc}")
    finally:
        client.close()


def _get_access_token() -> str:
    """Exchange client credentials for a bearer token via the Chalk REST API."""
    resp = _HTTP_CLIENT.post(
        f"{CHALK_API_SERVER}/v1/oauth/token",
        content={
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
    if not 200 <= resp.status < 300:
        raise RuntimeError(
            f"Token exchange failed with status {resp.status}: {resp.text()}"
        )
    return resp.json()["access_token"]


def _connect_rpc(
    service: str,
    method: str,
    body: dict,
    headers: dict[str, str],
    timeout: float = 30,
) -> FullResponse:
    """Make a Connect-protocol unary RPC call.

    Parameters
    ----------
    service
        Fully-qualified service name (e.g. ``"chalk.container.v1.ContainerService"``).
    method
        RPC method name on that service.
    """
    return _HTTP_CLIENT.post(
        f"{CHALK_API_SERVER}/{service}/{method}",
        content=body,
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
        if not 200 <= resp.status < 300:
            raise RuntimeError(
                f"ListScalingGroups failed with status {resp.status}: {resp.text()}"
            )
        page = resp.json()
        yield from page.get("scalingGroups", [])
        cursor = page.get("nextCursor") or ""
        if not cursor:
            return


def _sweep_stale_scoped_scaling_groups() -> None:
    """Delete CI-scoped scaling groups left behind by earlier builds.

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
        outcome = "deleted" if 200 <= resp.status < 300 else f"failed ({resp.status})"
        print(f"stale scaling group {name} ({created_at.isoformat()}): {outcome}")


def _sweep_stale_scoped_models() -> None:
    """Delete stale CI-scoped model versions and registrations."""
    from protobuf import Oneof

    from chalkcompute._connect import ConnectClient
    from chalkcompute._gen.chalk.server.v1 import (
        model_registry_connect,
        model_registry_pb,
    )

    client = ConnectClient()
    try:
        stub = client.stub(model_registry_connect.ModelRegistryServiceClientSync)
        now = datetime.now(timezone.utc)
        cursor = ""
        stale_models: list[tuple[str, datetime]] = []
        for _ in range(_LIST_PAGE_CAP):
            request: dict[str, object] = {"limit": _LIST_PAGE_SIZE}
            if cursor:
                request["cursor"] = cursor
            page = client.call(
                stub.list_models,
                model_registry_pb.ListModelsRequest(**request),
            )
            for model in page.models:
                name = model.model_name
                if not SCOPED_NAME_RE.match(name) or model.created_at is None:
                    continue
                created_at = model.created_at.to_datetime()
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                else:
                    created_at = created_at.astimezone(timezone.utc)
                if now - created_at < STALE_AFTER:
                    continue
                stale_models.append((name, created_at))
            cursor = page.next_cursor or ""
            if not cursor:
                break
        else:
            raise RuntimeError("ListModels exceeded the pagination page cap")

        for name, created_at in stale_models:
            try:
                _delete_model_versions(client, stub, name)
                client.call(
                    stub.delete_model,
                    model_registry_pb.DeleteModelRequest(
                        model_identifier=Oneof("model_name", name)
                    ),
                )
                print(f"stale model {name} ({created_at.isoformat()}): deleted")
            except Exception as exc:
                print(
                    f"stale model {name} ({created_at.isoformat()}): "
                    f"cleanup failed: {exc}"
                )
    finally:
        client.close()


def _delete_model_versions(client: Any, stub: Any, model_name: str) -> None:
    """Delete every version for a model, following the registry cursor."""
    from chalkcompute._gen.chalk.server.v1 import model_registry_pb

    cursor = ""
    versions: list[int] = []
    for _ in range(_LIST_PAGE_CAP):
        request: dict[str, object] = {
            "model_name": model_name,
            "limit": _LIST_PAGE_SIZE,
        }
        if cursor:
            request["cursor"] = cursor
        page = client.call(
            stub.list_model_versions,
            model_registry_pb.ListModelVersionsRequest(**request),
        )
        versions.extend(version.version for version in page.model_versions)
        cursor = page.next_cursor or ""
        if not cursor:
            break
    else:
        raise RuntimeError(
            f"ListModelVersions({model_name}) exceeded the pagination page cap"
        )

    for version in versions:
        client.call(
            stub.delete_model_version,
            model_registry_pb.DeleteModelVersionRequest(
                model_version_key=model_registry_pb.ModelVersionKey(
                    model_name=model_name,
                    version=version,
                )
            ),
        )


@pytest.fixture(scope="session", autouse=True)
def sweep_stale_ci_resources() -> None:
    """Reap scoped resources leaked by earlier builds before any test runs."""
    if not CHALK_CLIENT_ID or not CHALK_API_SERVER:
        return
    for sweep in (
        _sweep_stale_scoped_scaling_groups,
        _sweep_stale_scoped_models,
    ):
        try:
            sweep()
        except Exception as exc:
            print(f"warning: {sweep.__name__} skipped: {exc}")
