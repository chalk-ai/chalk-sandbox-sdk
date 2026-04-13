"""Shared Connect-protocol client for Chalk API services.

Provides auth (OAuth token exchange), Connect-protocol RPC dispatch,
image build polling, and volume file upload.
"""

from __future__ import annotations

import base64
import logging
import os
import time
import uuid
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

from chalkcompute._tracing import traced as _traced

if TYPE_CHECKING:
    import httpx

    from chalkcompute.image import Image, LazyLocalFile


class ConnectError(Exception):
    """Raised by ConnectClient operations."""


class ConnectClient:
    """Authenticated Connect-protocol (HTTP/JSON) client for Chalk APIs.

    Usage::

        client = ConnectClient()       # reads env vars lazily on first RPC
        resp = client.rpc("chalk.container.v1.ContainerService", "GetContainer", {"id": "..."})
    """

    def __init__(self) -> None:
        self._api_server: str | None = None
        self._access_token: str | None = None
        self._env_id: str | None = None
        self._headers: dict[str, str] | None = None

    def _ensure_auth(self) -> None:
        if self._access_token is not None:
            return

        import httpx

        self._api_server = os.environ.get("CHALK_API_SERVER", "")
        client_id = os.environ.get("CHALK_CLIENT_ID", "")
        client_secret = os.environ.get("CHALK_CLIENT_SECRET", "")
        self._env_id = os.environ.get("CHALK_ENVIRONMENT_ID", "")

        if not self._api_server or not client_id:
            raise ConnectError(
                "CHALK_API_SERVER and CHALK_CLIENT_ID must be set in the environment"
            )

        resp = httpx.post(
            f"{self._api_server}/v1/oauth/token",
            json={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
            },
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        self._access_token = resp.json()["access_token"]

        self._headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._access_token}",
            "X-Chalk-Env-Id": self._env_id or "",
        }

    def rpc(
        self,
        service: str,
        method: str,
        body: dict[str, Any],
        timeout: float = 30,
    ) -> httpx.Response:
        """Make a Connect-protocol RPC call."""
        import httpx

        from chalkcompute._tracing import get_tracer

        with get_tracer().start_as_current_span("chalkcompute.rpc") as span:
            span.set_attribute("rpc.service", service)
            span.set_attribute("rpc.method", method)
            self._ensure_auth()
            assert self._api_server is not None
            assert self._headers is not None
            resp = httpx.post(
                f"{self._api_server}/{service}/{method}",
                json=body,
                headers=self._headers,
                timeout=timeout,
            )
            span.set_attribute("http.response.status_code", resp.status_code)
            if resp.status_code >= 400:
                from opentelemetry.trace import StatusCode

                span.set_status(StatusCode.ERROR, f"HTTP {resp.status_code}")
            return resp


# ---------------------------------------------------------------------------
# Shared helpers that operate on a ConnectClient
# ---------------------------------------------------------------------------


@_traced("chalkcompute.build_image")
def build_image(
    client: ConnectClient,
    image: Image,
    *,
    poll_interval: float = 0.5,
    timeout: float = 600.0,
) -> str:
    """Build an Image spec via CustomImageService, return the image URI.

    Uses GetOrBuildCustomImage (returns cached image if available).
    """
    from google.protobuf.json_format import MessageToDict

    spec_dict = MessageToDict(image.to_proto())
    resp = client.rpc(
        "chalk.sandbox.v1.CustomImageService",
        "GetOrBuildCustomImage",
        {"imageSpec": spec_dict},
    )
    if resp.status_code != 200:
        raise ConnectError(f"GetOrBuildCustomImage failed: {resp.text}")

    data = resp.json()
    status = data.get("status", "")
    if status in ("exists", "succeeded"):
        return data["image"]
    if status == "failed":
        raise ConnectError(f"Image build failed: {data.get('error', 'unknown')}")

    build_id = data["buildId"]
    deadline = time.monotonic() + timeout
    transient_failures = 0
    max_transient_failures = 5
    while time.monotonic() < deadline:
        get_resp = client.rpc(
            "chalk.sandbox.v1.CustomImageService",
            "GetCustomImage",
            {"buildId": build_id},
        )
        if get_resp.status_code != 200:
            transient_failures += 1
            if transient_failures >= max_transient_failures:
                raise ConnectError(
                    f"GetCustomImage failed after {transient_failures} consecutive "
                    f"transient errors: {get_resp.text}"
                )
            logger.warning(
                "GetCustomImage transient error (%d/%d): %s",
                transient_failures, max_transient_failures, get_resp.text,
            )
            time.sleep(poll_interval)
            continue
        transient_failures = 0
        get_data = get_resp.json()
        status = get_data.get("status", "")
        if status == "succeeded":
            return get_data["image"]
        if status == "failed":
            raise ConnectError(
                f"Image build {build_id} failed: {get_data.get('error', 'unknown')}"
            )
        time.sleep(poll_interval)

    raise ConnectError(f"Image build {build_id} timed out after {timeout}s")


@_traced("chalkcompute.upload_lazy_files")
def upload_lazy_files(
    client: ConnectClient,
    image: Image,
    *,
    volume_prefix: str = "files",
) -> list[tuple[str, str]]:
    """Upload lazy local files to volumes. Returns [(volume_name, mount_path)]."""
    try:
        dir_to_files = image.lazy_local_files_grouped_for_volume_mounts()
    except ValueError as e:
        raise ConnectError(str(e)) from e

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _put_file(vol_name: str, lf: LazyLocalFile, dest_dir: str) -> None:
        from pathlib import Path

        content = Path(lf.src).read_bytes()
        encoded = base64.b64encode(content).decode("ascii")
        rel_path = os.path.relpath(lf.dest, dest_dir)
        put_resp = client.rpc(
            "chalk.volume.v1.VolumeService",
            "PutFile",
            {"volumeName": vol_name, "path": rel_path, "data": encoded},
        )
        if put_resp.status_code != 200:
            raise ConnectError(f"PutFile failed for {lf.dest}: {put_resp.text}")

    volumes: list[tuple[str, str]] = []
    for dest_dir, files in dir_to_files.items():
        vol_name = f"{volume_prefix}-{uuid.uuid4().hex[:12]}"
        resp = client.rpc(
            "chalk.volume.v1.VolumeService",
            "CreateVolume",
            {"name": vol_name},
        )
        if resp.status_code != 200:
            raise ConnectError(f"CreateVolume failed: {resp.text}")

        with ThreadPoolExecutor(max_workers=32) as pool:
            futures = [
                pool.submit(_put_file, vol_name, lf, dest_dir) for lf in files
            ]
            for fut in as_completed(futures):
                fut.result()

        volumes.append((vol_name, dest_dir))

    return volumes
