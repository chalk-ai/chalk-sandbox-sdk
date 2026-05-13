"""Shared Connect-protocol client for Chalk API services.

Provides auth (OAuth token exchange), typed Connect RPC dispatch, and image
build polling.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import urlparse

import grpc

from chalkcompute._tracing import traced as _traced

logger = logging.getLogger(__name__)

_SERVICE_AUTH_EXCHANGE_TOKEN_ENV_VAR = "CHALK_REMOTE_AUTH_EXCHANGE_TOKEN"
_SERVICE_AUTH_ENDPOINT_ENV_VAR = "CHALK_API_SERVER"
_SERVICE_AUTH_ENVIRONMENT_ENV_VAR = "CHALK_ENVIRONMENT_ID"
_SERVICE_AUTH_GRPC_ENGINE_ENV_VAR = "CHALK_GRPC_ENGINE"

_PERMISSION_AUTHENTICATED = 2
_PERMISSION_QUERY_ONLINE = 3
_PERMISSION_DEPLOY_READ = 11
_SERVICE_AUTH_REQUESTED_PERMISSIONS = (
    _PERMISSION_AUTHENTICATED,
    _PERMISSION_QUERY_ONLINE,
    _PERMISSION_DEPLOY_READ,
)

if TYPE_CHECKING:
    from chalkcompute._image import Image

_ResponseT = TypeVar("_ResponseT")


class ConnectError(Exception):
    """Raised by ConnectClient operations."""


def service_auth_config_from_env() -> dict[str, Any] | None:
    exchange_token = os.environ.get(_SERVICE_AUTH_EXCHANGE_TOKEN_ENV_VAR, "")
    if not exchange_token:
        return None

    auth_endpoint = os.environ.get(_SERVICE_AUTH_ENDPOINT_ENV_VAR, "")
    if not auth_endpoint:
        raise ConnectError(
            f"{_SERVICE_AUTH_ENDPOINT_ENV_VAR} must be set when "
            f"{_SERVICE_AUTH_EXCHANGE_TOKEN_ENV_VAR} is set"
        )

    environment_id = os.environ.get(_SERVICE_AUTH_ENVIRONMENT_ENV_VAR, "")
    if not environment_id:
        raise ConnectError(
            f"{_SERVICE_AUTH_ENVIRONMENT_ENV_VAR} must be set when "
            f"{_SERVICE_AUTH_EXCHANGE_TOKEN_ENV_VAR} is set"
        )

    return {
        "auth_endpoint": auth_endpoint.rstrip("/"),
        "exchange_token": exchange_token,
        "requested_permissions": _SERVICE_AUTH_REQUESTED_PERMISSIONS,
    }


def service_auth_environment() -> str:
    return os.environ.get(_SERVICE_AUTH_ENVIRONMENT_ENV_VAR, "")


def service_auth_grpc_engine_target() -> tuple[str, bool] | None:
    target = os.environ.get(_SERVICE_AUTH_GRPC_ENGINE_ENV_VAR, "")
    if not target:
        return None
    if target.startswith(("http://", "https://")):
        return grpc_target_from_url(target)
    return grpc_engine_target_from_url(target)


class ConnectClient:
    """Authenticated Connect-protocol client for Chalk APIs.

    Environment variables (``CHALK_API_SERVER``, ``CHALK_CLIENT_ID``,
    ``CHALK_CLIENT_SECRET``, ``CHALK_ENVIRONMENT_ID``) are read lazily on the
    first RPC.

    Examples
    --------
    >>> client = ConnectClient()
    >>> from chalkcompute._gen.chalk.container.v1 import service_connect, service_pb2
    >>> client.unary(
    ...     service_connect.ContainerServiceClientSync,
    ...     service_connect.ContainerServiceClientSync.get_container,
    ...     service_pb2.GetContainerRequest(id="..."),
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

        from chalkcompute._gen.chalk.server.v1 import (
            auth_connect,
            auth_pb2,
        )

        service_auth = service_auth_config_from_env()
        if service_auth is not None:
            self._api_server = str(service_auth["auth_endpoint"]).rstrip("/")
            token_resp = self._call_unary(
                auth_connect.AuthServiceClientSync,
                auth_connect.AuthServiceClientSync.get_internal_working_token,
                auth_pb2.GetInternalWorkingTokenRequest(
                    requested_permissions=list(service_auth["requested_permissions"])
                ),
                headers={
                    "Authorization": f"Bearer {service_auth['exchange_token']}",
                    "X-Chalk-Server": "go-api",
                },
                authenticate=False,
            )
            self._access_token = token_resp.access_token
            self._env_id = self._resolve_environment_id(service_auth_environment())
            self._headers = {
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

        token_resp = self._call_unary(
            auth_connect.AuthServiceClientSync,
            auth_connect.AuthServiceClientSync.get_token,
            auth_pb2.GetTokenRequest(
                client_id=client_id,
                client_secret=client_secret,
                grant_type="client_credentials",
            ),
            headers={
                "X-Chalk-Server": "go-api",
            },
            authenticate=False,
        )
        self._access_token = token_resp.access_token
        self._primary_environment = (
            token_resp.primary_environment
            if token_resp.HasField("primary_environment")
            else None
        )
        self._grpc_engines = dict(token_resp.grpc_engines)
        self._environment_id_to_name = dict(token_resp.environment_id_to_name)
        self._env_id = self._resolve_environment_id(
            requested_env or self._primary_environment or ""
        )

        self._headers = {
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

    def connect_headers(self) -> dict[str, str]:
        """Return authenticated headers for generated Connect clients."""
        self._ensure_auth()
        assert self._headers is not None
        return self._headers

    def unary(
        self,
        client_cls: type[Any],
        method: Callable[..., _ResponseT],
        request: Any,
        timeout: float | None = 30,
    ) -> _ResponseT:
        """Call a generated synchronous Connect unary method."""
        return self._call_unary(
            client_cls,
            method,
            request,
            timeout=timeout,
            authenticate=True,
        )

    def _call_unary(
        self,
        client_cls: type[Any],
        method: Callable[..., _ResponseT],
        request: Any,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = 30,
        authenticate: bool,
    ) -> _ResponseT:
        """Call a generated synchronous Connect unary method."""
        from connectrpc.errors import ConnectError as ConnectRPCError

        from chalkcompute._tracing import get_tracer

        method_name = getattr(method, "__name__", method.__class__.__name__)
        with get_tracer().start_as_current_span("chalkcompute.connect") as span:
            span.set_attribute("rpc.client", client_cls.__name__)
            span.set_attribute("rpc.method", method_name)
            if authenticate:
                self._ensure_auth()
                assert self._headers is not None
                headers = self._headers
            assert self._api_server is not None

            rpc_client = client_cls(
                self._api_server.rstrip("/"),
                send_compression=None,
            )
            timeout_ms = None if timeout is None else int(timeout * 1000)
            try:
                return method(
                    rpc_client,
                    request,
                    headers=headers,
                    timeout_ms=timeout_ms,
                )
            except ConnectRPCError as e:
                from opentelemetry.trace import StatusCode

                code = getattr(e, "code", None)
                code_name = getattr(code, "name", str(code))
                message = getattr(e, "message", str(e))
                span.set_status(StatusCode.ERROR, f"{code_name}: {message}")
                raise ConnectError(
                    f"{client_cls.__name__}.{method_name} failed: "
                    f"{code_name}: {message}"
                ) from e
            finally:
                rpc_client.close()


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
    from chalkcompute._gen.chalk.sandbox.v1 import (
        service_connect as sandbox_connect,
    )
    from chalkcompute._gen.chalk.sandbox.v1 import service_pb2 as sandbox_pb

    resp = client.unary(
        sandbox_connect.CustomImageServiceClientSync,
        sandbox_connect.CustomImageServiceClientSync.get_or_build_custom_image,
        sandbox_pb.GetOrBuildCustomImageRequest(image_spec=image.to_proto()),
    )

    status = resp.status
    if status in ("exists", "succeeded"):
        return resp.image
    if status == "failed":
        raise ConnectError(f"Image build failed: {resp.error or 'unknown'}")

    build_id = resp.build_id
    deadline = time.monotonic() + timeout
    transient_failures = 0
    max_transient_failures = 5
    while time.monotonic() < deadline:
        try:
            get_resp = client.unary(
                sandbox_connect.CustomImageServiceClientSync,
                sandbox_connect.CustomImageServiceClientSync.get_custom_image,
                sandbox_pb.GetCustomImageRequest(build_id=build_id),
            )
        except ConnectError as e:
            transient_failures += 1
            if transient_failures >= max_transient_failures:
                raise ConnectError(
                    f"GetCustomImage failed after {transient_failures} consecutive "
                    f"transient errors: {e}"
                ) from e
            logger.warning(
                "GetCustomImage transient error (%d/%d): %s",
                transient_failures,
                max_transient_failures,
                e,
            )
            time.sleep(poll_interval)
            continue
        transient_failures = 0
        status = get_resp.status
        if status == "succeeded":
            return get_resp.image
        if status == "failed":
            raise ConnectError(
                f"Image build {build_id} failed: {get_resp.error or 'unknown'}"
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
