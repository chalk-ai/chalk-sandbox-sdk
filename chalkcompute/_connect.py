"""Shared Connect-protocol client for Chalk API services.

Provides auth (OAuth token exchange), Connect-protocol RPC dispatch,
and image build polling.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import TYPE_CHECKING, Any, Mapping
from urllib.parse import urlparse

import grpc

logger = logging.getLogger(__name__)

_SERVICE_AUTH_EXCHANGE_TOKEN_ENV_VARS = (
    "CHALK_REMOTE_AUTH_EXCHANGE_TOKEN",
    "CHALK_EXCHANGE_TOKEN",
)
_SERVICE_AUTH_ENDPOINT_ENV_VARS = (
    "CHALK_REMOTE_AUTH_ENDPOINT",
    "CHALK_API_SERVER",
    "_CHALK_API_SERVER",
)
_SERVICE_AUTH_ENVIRONMENT_ENV_VARS = (
    "CHALK_ENVIRONMENT_ID",
    "_CHALK_ACTIVE_ENVIRONMENT",
    "CHALK_ENVIRONMENT",
)
_SERVICE_AUTH_GRPC_ENGINE_ENV_VARS = (
    "CHALK_GRPC_ENGINE",
    "CHALK_REMOTE_GRPC_ENDPOINT",
    "CHALK_REMOTE_CALL_GRPC_ENDPOINT",
)
_SERVICE_AUTH_REQUESTED_PERMISSIONS = (
    2,  # PERMISSION_AUTHENTICATED
    3,  # PERMISSION_QUERY_ONLINE
    11,  # PERMISSION_DEPLOY_READ
)

if TYPE_CHECKING:
    import httpx

    from chalkcompute._image import Image


class ConnectError(Exception):
    """Raised by ConnectClient operations."""


def _first_env(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.environ.get(name, "")
        if value:
            return value
    return ""


def service_auth_config_from_env() -> dict[str, Any] | None:
    exchange_token = _first_env(_SERVICE_AUTH_EXCHANGE_TOKEN_ENV_VARS)
    if not exchange_token:
        return None

    auth_endpoint = _first_env(_SERVICE_AUTH_ENDPOINT_ENV_VARS)
    if not auth_endpoint:
        raise ConnectError(
            "CHALK_REMOTE_AUTH_ENDPOINT or CHALK_API_SERVER must be set when "
            "CHALK_REMOTE_AUTH_EXCHANGE_TOKEN is set"
        )

    return {
        "auth_endpoint": auth_endpoint.rstrip("/"),
        "exchange_token": exchange_token,
        "requested_permissions": _SERVICE_AUTH_REQUESTED_PERMISSIONS,
    }


def service_auth_environment() -> str:
    return _first_env(_SERVICE_AUTH_ENVIRONMENT_ENV_VARS)


def service_auth_grpc_engine_target() -> tuple[str, bool] | None:
    target = _first_env(_SERVICE_AUTH_GRPC_ENGINE_ENV_VARS)
    if not target:
        return None
    if target.startswith(("http://", "https://")):
        return grpc_target_from_url(target)
    return grpc_engine_target_from_url(target)


class ConnectClient:
    """Authenticated Connect-protocol (HTTP/JSON) client for Chalk APIs.

    Environment variables (``CHALK_API_SERVER``, ``CHALK_CLIENT_ID``,
    ``CHALK_CLIENT_SECRET``, ``CHALK_ENVIRONMENT_ID``) are read lazily on the
    first RPC.

    Examples
    --------
    >>> client = ConnectClient()
    >>> resp = client.rpc(
    ...     "chalk.container.v1.ContainerService",
    ...     "GetContainer",
    ...     {"id": "..."},
    ... )
    """

    def __init__(self) -> None:
        self._api_server: str | None = None
        self._access_token: str | None = None
        self._env_id: str | None = None
        self._headers: dict[str, str] | None = None
        self._grpc_engines: dict[str, str] = {}
        self._environment_id_to_name: dict[str, str] = {}
        self._primary_environment: str | None = None

    def _ensure_auth(self) -> None:
        if self._access_token is not None:
            return

        import httpx

        service_auth = service_auth_config_from_env()
        if service_auth is not None:
            self._api_server = str(service_auth["auth_endpoint"]).rstrip("/")
            resp = httpx.post(
                f"{self._api_server}/chalk.server.v1.AuthService/GetInternalWorkingToken",
                json={
                    "requestedPermissions": list(
                        service_auth["requested_permissions"]
                    ),
                },
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {service_auth['exchange_token']}",
                    "Content-Type": "application/json",
                    "X-Chalk-Server": "go-api",
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            self._access_token = data.get("accessToken") or data["access_token"]
            self._env_id = self._resolve_environment_id(service_auth_environment())
            self._headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._access_token}",
                "X-Chalk-Env-Id": self._env_id or "",
                "X-Chalk-Server": "go-api",
            }
            return

        self._api_server = os.environ.get("CHALK_API_SERVER", "")
        client_id = os.environ.get("CHALK_CLIENT_ID", "")
        client_secret = os.environ.get("CHALK_CLIENT_SECRET", "")
        requested_env = (
            os.environ.get("CHALK_ENVIRONMENT_ID")
            or os.environ.get("CHALK_ENVIRONMENT")
            or ""
        )

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
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Chalk-Server": "go-api",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        self._primary_environment = data.get("primary_environment")
        self._grpc_engines = _string_map(data.get("grpc_engines"))
        self._environment_id_to_name = _string_map(
            data.get("environment_id_to_name")
        )
        self._env_id = self._resolve_environment_id(
            requested_env or self._primary_environment or ""
        )

        self._headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._access_token}",
            "X-Chalk-Env-Id": self._env_id or "",
            "X-Chalk-Server": "go-api",
        }

    def _resolve_environment_id(self, environment: str) -> str:
        if not environment:
            return ""
        if (
            environment in self._grpc_engines
            or environment in self._environment_id_to_name
        ):
            return environment

        lower_environment = environment.lower()
        matches = [
            env_id
            for env_id, env_name in self._environment_id_to_name.items()
            if env_name.lower() == lower_environment
        ]
        if len(matches) > 1:
            raise ConnectError(
                f"Multiple Chalk environments match {environment!r}: {matches}"
            )
        if len(matches) == 1:
            return matches[0]
        return environment

    def grpc_engine_target(self) -> tuple[str, bool]:
        """Return the active environment's gRPC engine target and TLS mode."""
        self._ensure_auth()
        if not self._env_id:
            raise ConnectError("No Chalk environment selected")

        query_server = self._grpc_engines.get(self._env_id)
        if not query_server:
            available = ", ".join(sorted(self._grpc_engines)) or "none"
            raise ConnectError(
                "No gRPC engine endpoint found for Chalk environment "
                f"{self._env_id!r}. Available environments: {available}"
            )

        return grpc_engine_target_from_url(query_server)

    def rpc(
        self,
        service: str,
        method: str,
        body: dict[str, Any],
        timeout: float = 30,
        extra_headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Make a Connect-protocol RPC call.

        Parameters
        ----------
        service
            Fully-qualified service name (e.g. ``chalk.container.v1.ContainerService``).
        method
            Method name on the service.
        body
            JSON-serializable request body.
        timeout
            Request timeout in seconds.
        extra_headers
            Additional request headers. These are merged per call and do not
            mutate the client's cached auth headers.

        Returns
        -------
        httpx.Response
            The raw HTTP response from the Connect endpoint.
        """
        import httpx

        self._ensure_auth()
        assert self._api_server is not None
        assert self._headers is not None
        headers = self._headers
        if extra_headers:
            headers = {**headers, **extra_headers}
        return httpx.post(
            f"{self._api_server}/{service}/{method}",
            json=body,
            headers=headers,
            timeout=timeout,
        )


# ---------------------------------------------------------------------------
# Shared helpers that operate on a ConnectClient
# ---------------------------------------------------------------------------


def build_image(
    client: ConnectClient,
    image: Image,
    *,
    poll_interval: float = 0.5,
    timeout: float = 600.0,
) -> str:
    """Build an ``Image`` spec via ``CustomImageService`` and return its URI.

    Uses ``GetOrBuildCustomImage`` which returns a cached image if one is
    available.

    Parameters
    ----------
    client
        Authenticated Connect client.
    image
        The declarative image definition to build.
    poll_interval
        Seconds between build-status polls.
    timeout
        Maximum seconds to wait for the build to complete.

    Returns
    -------
    str
        The built image URI.

    Raises
    ------
    ConnectError
        If the build fails or times out.
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
                transient_failures,
                max_transient_failures,
                get_resp.text,
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


def build_image_from_spec(image: Image) -> str:
    """Build an Image and return the URI. Authenticates via env vars."""
    client = ConnectClient()
    return build_image(client, image)


def raise_for_grpc_error(
    e: grpc.RpcError,
    code_map: dict[grpc.StatusCode, tuple[type[Exception], str]],
) -> None:
    """Translate a gRPC error to a typed exception if its status code is mapped.

    Parameters
    ----------
    e
        The gRPC error raised by a stub call.
    code_map
        Mapping from ``grpc.StatusCode`` to ``(exception_class, message)``
        pairs.  Status codes not in the map are ignored — the caller
        should re-raise the original error.
    """
    assert isinstance(e, grpc.Call)
    mapped = code_map.get(e.code())
    if mapped is not None:
        exc_cls, message = mapped
        raise exc_cls(message) from e


def grpc_target_from_url(
    url: str,
    *,
    insecure_port: int | None = None,
) -> tuple[str, bool]:
    """Convert an HTTP-style URL into a gRPC ``host:port`` target.

    Parameters
    ----------
    url
        URL or host string to convert.
    insecure_port
        Port to use for ``http://`` URLs when none is explicitly present.

    Returns
    -------
    tuple[str, bool]
        ``(target, use_tls)`` where ``target`` is a gRPC dial target.
    """
    use_tls = url.startswith("https://")
    target = re.sub(r"^https?://", "", url).rstrip("/")
    host = target.split("/")[0]

    if ":" in host:
        return host, use_tls

    if use_tls:
        return f"{host}:443", True
    if insecure_port is not None:
        return f"{host}:{insecure_port}", False
    return host, False


def grpc_engine_target_from_url(url: str) -> tuple[str, bool]:
    """Parse a Chalk engine URI using Chalk's external-TLS/local-insecure rule."""
    if url.startswith(("localhost", "127.0.0.1", "10.")):
        use_tls = False
        target = url
    else:
        parsed = urlparse(url)
        if parsed.scheme == "http":
            use_tls = False
        elif parsed.scheme == "https":
            use_tls = True
        else:
            use_tls = True

        target = url.removeprefix(parsed.scheme + "://") if parsed.scheme else url
    return target.rstrip("/").split("/")[0], use_tls


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}
