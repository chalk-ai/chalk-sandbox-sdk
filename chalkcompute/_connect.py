"""Shared authenticated client for Chalk API services.

Provides auth (OAuth token exchange), typed RPC dispatch through ConnectRPC
clients via ``stub()``/``call()``, plus the legacy untyped Connect JSON
``rpc()`` helper and image build polling.
"""

from __future__ import annotations

import logging
import os
import re
import time
from threading import RLock
from dataclasses import dataclass, field
from chalkcompute._call_context import current_invocation
from typing import TYPE_CHECKING, Any, Callable, Mapping, TypeVar
from urllib.parse import urlparse

from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError
from connectrpc.protocol import ProtocolType

from chalkcompute._auth_config import active_environment_from_env, load_token
from chalkcompute._gen.chalk.auth.v1 import permissions_pb
from chalkcompute._gen.chalk.sandbox.v1 import service_connect as image_connect
from chalkcompute._gen.chalk.sandbox.v1 import service_pb as image_pb
from chalkcompute._gen.chalk.server.v1 import auth_connect, auth_pb
from chalkcompute._http import SYNC_HTTP_CLIENT
from chalkcompute._remote_call_client import (
    RemoteCallClientUnavailable,
    ServiceAuthContext,
    resolve_service_auth_context,
)
from chalkcompute._rpc import RPC_READ_MAX_BYTES, rpc_interceptors
from chalkcompute._tracing import _instrumented
from chalkcompute._web_identity import (
    WebIdentityTokenError,
    read_web_identity_token,
    web_identity_token_file_from_env,
)

logger = logging.getLogger(__name__)

_WEB_IDENTITY_MAX_CACHE_TTL_SECONDS = 60 * 60

_StubT = TypeVar("_StubT")
_RespT = TypeVar("_RespT")


if TYPE_CHECKING:
    from chalkcompute._image import Image
    from chalkcompute._volume import VolumeMount


class ConnectError(Exception):
    """Raised by ConnectClient operations."""


def service_auth_context_from_env() -> ServiceAuthContext | None:
    invocation = current_invocation()
    if invocation is not None or os.environ.get("CHALK_FUNCTION_AUTH_MODE") == "caller":
        if invocation is None or not invocation.exchange_token:
            raise ConnectError(
                "This function invocation has no delegated caller credentials"
            )
        endpoint = os.environ.get("CHALK_API_SERVER")
        environment = os.environ.get("CHALK_ENVIRONMENT_ID")
        if not endpoint or not environment:
            raise ConnectError(
                "Chalk function runtime is missing its API server or environment"
            )
        return ServiceAuthContext(
            auth_endpoint=endpoint,
            exchange_token=invocation.exchange_token,
            environment_id=environment,
            requested_permissions=(),
            grpc_engine=None,
        )
    if os.environ.get("CHALK_FUNCTION_AUTH_MODE") == "service":
        if not (
            (
                os.environ.get("CHALK_CLIENT_ID")
                and os.environ.get("CHALK_CLIENT_SECRET")
            )
            or web_identity_token_file_from_env() is not None
        ):
            raise ConnectError(
                "Service-authenticated functions require explicit service credentials"
            )
        return None
    try:
        return resolve_service_auth_context()
    except RemoteCallClientUnavailable as exc:
        logger.debug("service-auth resolver unavailable: %s", exc)
        return None


def chalk_client_from_env(
    *,
    api_server: str | None = None,
    environment: str | None = None,
    additional_headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> Any:
    if (
        current_invocation() is not None
        or os.environ.get("CHALK_FUNCTION_AUTH_MODE") == "caller"
    ):
        raise ConnectError(
            "chalk_client_from_env cannot provide invocation-scoped authentication to "
            "ChalkPy; use chalkcompute clients inside caller-authenticated functions"
        )
    from chalk.client import ChalkClient

    headers = additional_headers
    has_authorization = any(key.lower() == "authorization" for key in headers or {})
    # Web-identity rotation is implemented by this SDK's own transports.
    # A ChalkClient built here would cache the injected header indefinitely.
    service_auth = (
        None
        if ConnectClient()._uses_web_identity_token()
        else service_auth_context_from_env()
    )
    if service_auth is not None and not has_authorization:
        client = ConnectClient()
        client._ensure_auth()
        assert client._access_token is not None
        assert client._api_server is not None
        headers = {
            **(headers or {}),
            "Authorization": f"Bearer {client._access_token}",
        }
        api_server = api_server or client._api_server
        environment = environment or service_auth.environment_id

    if api_server is not None:
        kwargs["api_server"] = api_server
    if environment is not None:
        kwargs["environment"] = environment
    kwargs["additional_headers"] = headers
    return ChalkClient(**kwargs)


@dataclass
class _AuthState:
    _api_server: str | None = None
    _access_token: str | None = None
    _env_id: str | None = None
    _headers: dict[str, str] | None = None
    _grpc_engines: dict[str, str] = field(default_factory=dict)
    _environment_id_to_name: dict[str, str] = field(default_factory=dict)
    _primary_environment: str | None = None
    _service_auth_refresh_at: float | None = None
    _web_identity_token_file: str | None = None
    _web_identity_cached_at: float | None = None
    _web_identity_cache_ttl_seconds: float = _WEB_IDENTITY_MAX_CACHE_TTL_SECONDS


class ConnectClient:
    """Authenticated RPC client for Chalk APIs.

    Typed RPC dispatch goes through ConnectRPC clients using the gRPC protocol
    over the shared HTTP/2 transport. Ambient Chalk credentials are read lazily.
    A JWT configured through ``CHALK_WEB_IDENTITY_TOKEN_FILE`` takes precedence
    and is cached for the shorter of one hour or half its remaining lifetime.
    Otherwise, service auth or ChalkPy's client credentials and ``~/.chalk.yml``
    lookup are used.

    Examples
    --------
    >>> client = ConnectClient()
    >>> stub = client.stub(ContainerServiceStub)
    >>> resp = client.call(stub.GetContainer, pb.GetContainerRequest(id="..."))
    """

    def __init__(
        self,
        chalk_client: Any | None = None,
    ) -> None:
        self._local_auth_lock = RLock()
        self._local_auth_state = _AuthState()
        self._chalk_client = chalk_client
        self._typed_clients: dict[Callable[..., Any], Any] = {}

    @property
    def _auth_lock(self) -> Any:
        scope = current_invocation()
        return scope.auth_lock if scope is not None else self._local_auth_lock

    def _auth_state(self) -> _AuthState:
        scope = current_invocation()
        if scope is None:
            return self._local_auth_state
        with self._auth_lock:
            if ConnectClient not in scope.auth_states:
                scope.auth_states[ConnectClient] = _AuthState()
            return scope.auth_states[ConnectClient]

    @property
    def _api_server(self) -> str | None:
        return self._auth_state()._api_server

    @_api_server.setter
    def _api_server(self, value: str | None) -> None:
        self._auth_state()._api_server = value

    @property
    def _access_token(self) -> str | None:
        return self._auth_state()._access_token

    @_access_token.setter
    def _access_token(self, value: str | None) -> None:
        self._auth_state()._access_token = value

    @property
    def _env_id(self) -> str | None:
        return self._auth_state()._env_id

    @_env_id.setter
    def _env_id(self, value: str | None) -> None:
        self._auth_state()._env_id = value

    @property
    def _headers(self) -> dict[str, str] | None:
        return self._auth_state()._headers

    @_headers.setter
    def _headers(self, value: dict[str, str] | None) -> None:
        self._auth_state()._headers = value

    @property
    def _grpc_engines(self) -> dict[str, str]:
        return self._auth_state()._grpc_engines

    @_grpc_engines.setter
    def _grpc_engines(self, value: dict[str, str]) -> None:
        self._auth_state()._grpc_engines = value

    @property
    def _environment_id_to_name(self) -> dict[str, str]:
        return self._auth_state()._environment_id_to_name

    @_environment_id_to_name.setter
    def _environment_id_to_name(self, value: dict[str, str]) -> None:
        self._auth_state()._environment_id_to_name = value

    @property
    def _primary_environment(self) -> str | None:
        return self._auth_state()._primary_environment

    @_primary_environment.setter
    def _primary_environment(self, value: str | None) -> None:
        self._auth_state()._primary_environment = value

    @property
    def _service_auth_refresh_at(self) -> float | None:
        return self._auth_state()._service_auth_refresh_at

    @_service_auth_refresh_at.setter
    def _service_auth_refresh_at(self, value: float | None) -> None:
        self._auth_state()._service_auth_refresh_at = value

    @property
    def _web_identity_token_file(self) -> str | None:
        return self._auth_state()._web_identity_token_file

    @_web_identity_token_file.setter
    def _web_identity_token_file(self, value: str | None) -> None:
        self._auth_state()._web_identity_token_file = value

    @property
    def _web_identity_cached_at(self) -> float | None:
        return self._auth_state()._web_identity_cached_at

    @_web_identity_cached_at.setter
    def _web_identity_cached_at(self, value: float | None) -> None:
        self._auth_state()._web_identity_cached_at = value

    @property
    def _web_identity_cache_ttl_seconds(self) -> float:
        return self._auth_state()._web_identity_cache_ttl_seconds

    @_web_identity_cache_ttl_seconds.setter
    def _web_identity_cache_ttl_seconds(self, value: float) -> None:
        self._auth_state()._web_identity_cache_ttl_seconds = value

    def _ensure_auth(self) -> None:
        with self._auth_lock:
            self._authenticate()

    def _authenticate(self) -> None:
        identity_token_file = (
            None
            if current_invocation() is not None
            or os.environ.get("CHALK_FUNCTION_AUTH_MODE") == "caller"
            else web_identity_token_file_from_env()
        )
        if identity_token_file is not None:
            if (
                identity_token_file == self._web_identity_token_file
                and self._access_token is not None
                and self._web_identity_cached_at is not None
                and time.monotonic() - self._web_identity_cached_at
                < self._web_identity_cache_ttl_seconds
            ):
                return
            try:
                identity = read_web_identity_token(identity_token_file)
            except WebIdentityTokenError as exc:
                raise ConnectError(str(exc)) from exc
            requested_env = active_environment_from_env()
            self._api_server = (
                os.environ.get("CHALK_API_SERVER") or "https://api.chalk.ai"
            ).rstrip("/")
            self._access_token = identity.value
            self._env_id = requested_env or identity.environment_id or ""
            self._headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {identity.value}",
                "X-Chalk-Env-Id": self._env_id or "",
                "X-Chalk-Server": "go-api",
            }
            self._grpc_engines = {}
            self._environment_id_to_name = {}
            self._primary_environment = None
            self._web_identity_token_file = identity_token_file
            self._web_identity_cached_at = time.monotonic()
            self._web_identity_cache_ttl_seconds = (
                _WEB_IDENTITY_MAX_CACHE_TTL_SECONDS
                if identity.expires_at is None
                else min(
                    _WEB_IDENTITY_MAX_CACHE_TTL_SECONDS,
                    max(0.0, identity.expires_at - time.time()) / 2,
                )
            )
            return

        if self._access_token is not None:
            if self._service_auth_refresh_at is None:
                return
            if time.monotonic() < self._service_auth_refresh_at:
                return

        service_auth = service_auth_context_from_env()
        if service_auth is None and self._service_auth_refresh_at is not None:
            raise ConnectError(
                "Chalk runtime identity is unavailable during token refresh"
            )
        if service_auth is not None:
            self._api_server = service_auth.auth_endpoint.rstrip("/")
            auth_client = auth_connect.AuthServiceClientSync(
                service_auth.auth_endpoint.rstrip("/"),
                protocol=ProtocolType.GRPC,
                interceptors=rpc_interceptors(),
                http_client=SYNC_HTTP_CLIENT,
                read_max_bytes=RPC_READ_MAX_BYTES,
            )
            try:
                data = auth_client.get_internal_working_token(
                    auth_pb.GetInternalWorkingTokenRequest(
                        requested_permissions=[
                            permissions_pb.Permission(value)
                            for value in service_auth.requested_permissions
                        ],
                    ),
                    headers={
                        "Authorization": f"Bearer {service_auth.exchange_token}",
                        "X-Chalk-Server": "go-api",
                    },
                    timeout_ms=30_000,
                )
            finally:
                auth_client.close()
            if not data.access_token:
                raise ConnectError("Working-token exchange returned no access token")
            # Missing expiry is never cached; otherwise refresh before expiry,
            # with a smaller margin for unusually short-lived tokens.
            ttl = (
                data.expires_at.seconds + data.expires_at.nanos / 1e9 - time.time()
                if data.expires_at is not None
                else 0.0
            )
            if data.expires_at is not None and ttl <= 0:
                raise ConnectError("Working-token exchange returned an expired token")
            self._service_auth_refresh_at = time.monotonic() + max(
                0.0, ttl - min(60.0, ttl / 2)
            )
            self._access_token = data.access_token
            self._grpc_engines = _string_map(data.grpc_engines or data.engines)
            self._environment_id_to_name = _string_map(data.environment_id_to_name)
            self._env_id = self._resolve_environment_id(service_auth.environment_id)
            self._headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._access_token}",
                "X-Chalk-Env-Id": self._env_id or "",
                "X-Chalk-Server": "go-api",
            }
            return

        token = (
            None
            if self._chalk_client is not None
            else load_token(None, None, None, None)
        )
        self._api_server = (
            token.apiServer
            if token is not None
            else os.environ.get("CHALK_API_SERVER", "")
        )
        client_id = (
            token.clientId
            if token is not None
            else os.environ.get("CHALK_CLIENT_ID", "")
        )
        client_secret = (
            token.clientSecret
            if token is not None
            else os.environ.get("CHALK_CLIENT_SECRET", "")
        )
        requested_env = (
            token.activeEnvironment
            if token is not None
            else active_environment_from_env() or ""
        )

        if self._chalk_client is not None:
            cc = self._chalk_client
            if cc._client_id is None or cc._client_secret is None:
                if hasattr(cc, "_exchange_credentials"):
                    cc._exchange_credentials()
            if not self._api_server:
                self._api_server = cc._api_server or ""
            if not client_id:
                client_id = cc._client_id or ""
            if not client_secret:
                client_secret = cc._client_secret or ""
            if not requested_env:
                requested_env = (
                    getattr(cc, "_primary_environment", None)
                    or getattr(cc, "_environment", None)
                    or ""
                )

        if not self._api_server or not client_id or not client_secret:
            raise ConnectError(
                "No Chalk credentials found. Set CHALK_CLIENT_ID/CHALK_CLIENT_SECRET "
                "environment variables, run `chalk login`, or pass a ChalkClient instance."
            )

        auth_client = auth_connect.AuthServiceClientSync(
            self._api_server,
            protocol=ProtocolType.GRPC,
            interceptors=rpc_interceptors(),
            http_client=SYNC_HTTP_CLIENT,
            read_max_bytes=RPC_READ_MAX_BYTES,
        )
        try:
            data = auth_client.get_token(
                auth_pb.GetTokenRequest(
                    client_id=client_id,
                    client_secret=client_secret,
                    grant_type="client_credentials",
                ),
                headers={"X-Chalk-Server": "go-api"},
                timeout_ms=30_000,
            )
        finally:
            auth_client.close()
        self._access_token = data.access_token
        self._primary_environment = data.primary_environment or None
        self._grpc_engines = _string_map(data.grpc_engines or data.engines)
        self._environment_id_to_name = _string_map(data.environment_id_to_name)
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

        if self._uses_web_identity_token():
            query_server = os.environ.get("CHALK_GRPC_ENGINE", "").strip()
            if not query_server:
                raise ConnectError(
                    "CHALK_GRPC_ENGINE must be set for queued function calls when "
                    "CHALK_WEB_IDENTITY_TOKEN_FILE is used because Chalk identity "
                    "JWTs do not contain gRPC engine routing"
                )
            return grpc_engine_target_from_url(query_server)

        query_server = self._grpc_engines.get(self._env_id)
        if not query_server:
            available = ", ".join(sorted(self._grpc_engines)) or "none"
            raise ConnectError(
                "No gRPC engine endpoint found for Chalk environment "
                f"{self._env_id!r}. Available environments: {available}"
            )

        return grpc_engine_target_from_url(query_server)

    def _rpc_metadata(self) -> list[tuple[str, str]]:
        """Return RPC metadata after refreshing ambient credentials if needed."""
        with self._auth_lock:
            self._ensure_auth()
            return [
                ("authorization", f"Bearer {self._access_token}"),
                ("x-chalk-env-id", self._env_id or ""),
                ("x-chalk-server", "go-api"),
            ]

    def _uses_web_identity_token(self) -> bool:
        """Return whether rotating web-identity authentication is configured."""
        return (
            current_invocation() is None
            and os.environ.get("CHALK_FUNCTION_AUTH_MODE") != "caller"
            and web_identity_token_file_from_env() is not None
        )

    def function_invocation_metadata(self, function_name: str) -> list[tuple[str, str]]:
        """Delegate to the function without forwarding the caller's original token."""
        stub = self.stub(auth_connect.AuthServiceClientSync)
        response = self.call(
            stub.create_function_invocation_token,
            auth_pb.CreateFunctionInvocationTokenRequest(function_name=function_name),
        )
        if not response.exchange_token:
            raise ConnectError("Function delegation returned no exchange token")
        working = self.call(
            stub.get_internal_working_token,
            auth_pb.GetInternalWorkingTokenRequest(),
            extra_headers={"authorization": f"Bearer {response.exchange_token}"},
        )
        if not working.access_token:
            raise ConnectError("Function delegation returned no working token")
        return [
            ("x-chalk-function-invocation-token", response.exchange_token),
            ("authorization", f"Bearer {working.access_token}"),
        ]

    def get_workload_identity_token(
        self,
        audience: str,
        *,
        timeout: float = 30,
    ) -> str:
        """Return a short-lived OIDC token for workload identity federation.

        The token is scoped to the active Chalk environment and uses ``audience``
        as its OIDC audience. Third parties can validate it against the Chalk
        API server's ``/.well-known/jwks.json`` endpoint.

        Parameters
        ----------
        audience
            Audience expected by the workload identity provider.
        timeout
            Request timeout in seconds.
        """
        if not isinstance(audience, str) or not audience.strip():
            raise ValueError("audience cannot be empty")

        stub = self.stub(auth_connect.AuthServiceClientSync)
        response = self.call(
            stub.get_workload_identity_token,
            auth_pb.GetWorkloadIdentityTokenRequest(audience=audience),
            timeout=timeout,
        )
        if not isinstance(response.token, str) or not response.token:
            raise ConnectError(
                "GetWorkloadIdentityToken returned a response without a token"
            )
        return response.token

    def stub(self, stub_cls: Callable[..., _StubT]) -> _StubT:
        """Return a generated ConnectRPC client for the API server.

        Clients always use the gRPC protocol over the shared HTTP/2 transport.
        """
        self._ensure_auth()
        assert self._api_server is not None
        client = self._typed_clients.get(stub_cls)
        if client is None:
            client = stub_cls(
                self._api_server,
                protocol=ProtocolType.GRPC,
                interceptors=rpc_interceptors(),
                http_client=SYNC_HTTP_CLIENT,
                read_max_bytes=RPC_READ_MAX_BYTES,
            )
            self._typed_clients[stub_cls] = client
        return client

    def close(self) -> None:
        """Close cached typed ConnectRPC clients."""
        for client in self._typed_clients.values():
            close = getattr(client, "close", None)
            if close is not None:
                close()
        self._typed_clients.clear()

    def call(
        self,
        method: Callable[..., _RespT],
        request: Any,
        *,
        timeout: float = 30.0,
        extra_headers: Mapping[str, str] | None = None,
    ) -> _RespT:
        """Invoke a generated unary RPC method with per-call auth headers.

        Metadata is rebuilt on every call via :meth:`_rpc_metadata`, so
        rotating web-identity tokens are picked up without any caller effort.

        Parameters
        ----------
        method
            A bound client method, e.g. ``client.stub(FooClient).get_foo``.
        request
            The request protobuf message.
        timeout
            RPC deadline in seconds.

        """
        headers = dict(self._rpc_metadata())
        if extra_headers:
            headers.update(extra_headers)
        return method(
            request,
            timeout_ms=round(timeout * 1000),
            headers=headers,
        )


# ---------------------------------------------------------------------------
# Shared helpers that operate on a ConnectClient
# ---------------------------------------------------------------------------


@_instrumented("chalkcompute.build_image")
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
    image_uri = image.as_image_uri_if_possible()
    if image_uri is not None:
        return image_uri

    stub = client.stub(image_connect.CustomImageServiceClientSync)
    response = client.call(
        stub.get_or_build_custom_image,
        image_pb.GetOrBuildCustomImageRequest(image_spec=image.to_proto()),
    )
    status = response.status
    if status in ("exists", "succeeded"):
        return response.image
    if status == "failed":
        raise ConnectError(f"Image build failed: {response.error or 'unknown'}")

    return wait_for_image_build(
        client, response.build_id, poll_interval=poll_interval, timeout=timeout
    )


def wait_for_image_build(
    client: ConnectClient, build_id: str, *, poll_interval: float, timeout: float
) -> str:
    """Wait for an already-submitted build without another get-or-build call."""
    stub = client.stub(image_connect.CustomImageServiceClientSync)
    deadline = time.monotonic() + timeout
    transient_failures = 0
    max_transient_failures = 5
    while time.monotonic() < deadline:
        try:
            get_response = client.call(
                stub.get_custom_image,
                image_pb.GetCustomImageRequest(build_id=build_id),
            )
        except ConnectRpcError as exc:
            if exc.code == Code.NOT_FOUND:
                # Image retention outlives the workflow record. The ensure
                # protocol can recover by checking the retained image.
                raise
            transient_failures += 1
            if transient_failures >= max_transient_failures:
                raise ConnectError(
                    f"GetCustomImage failed after {transient_failures} consecutive "
                    f"transient errors: {exc}"
                ) from exc
            logger.warning(
                "GetCustomImage transient error (%d/%d): %s",
                transient_failures,
                max_transient_failures,
                exc,
            )
            time.sleep(poll_interval)
            continue
        transient_failures = 0
        status = get_response.status
        if status == "succeeded":
            return get_response.image
        if status == "failed":
            raise ConnectError(
                f"Image build {build_id} failed: {get_response.error or 'unknown'}"
            )
        time.sleep(poll_interval)

    raise ConnectError(f"Image build {build_id} timed out after {timeout}s")


def build_image_from_spec(image: Image, chalk_client: Any | None = None) -> str:
    """Build an Image and return the URI. Authenticates via env vars."""
    client = ConnectClient(chalk_client=chalk_client)
    return build_image(client, image)


@_instrumented("chalkcompute.build_image_with_volumes")
def build_image_with_volumes(
    client: ConnectClient,
    image: Image,
    *,
    poll_interval: float = 0.5,
    timeout: float = 600.0,
    volume_prefix: str = "files",
) -> tuple[str, list[VolumeMount]]:
    """Build an ``Image`` and upload any lazy local files to volumes.

    Combines :func:`build_image` (build via ``CustomImageService``) with
    :meth:`chalkcompute._volume.VolumeClient.upload_lazy_files` (upload
    ``add_local_file`` / ``add_local_dir`` entries registered with
    ``strategy="volume"`` to managed volumes).

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
    volume_prefix
        Prefix used when naming the generated volumes.

    Returns
    -------
    tuple[str, list[VolumeMount]]
        ``(image_uri, volumes)`` where ``volumes`` is a list of
        :class:`VolumeMount` ready to attach to a container spec.

    Raises
    ------
    ConnectError
        If the build fails or times out.
    """
    from chalkcompute._volume import VolumeClient

    image_uri = build_image(
        client,
        image,
        poll_interval=poll_interval,
        timeout=timeout,
    )
    volumes: list[VolumeMount] = []
    if image.lazy_local_files:
        volumes = VolumeClient.from_connect(client).upload_lazy_files(
            image,
            volume_prefix=volume_prefix,
        )
    return image_uri, volumes


def build_image_with_volumes_from_spec(
    image: Image,
    *,
    volume_prefix: str = "files",
    chalk_client: Any | None = None,
) -> tuple[str, list[VolumeMount]]:
    """Build an Image and upload lazy files. Authenticates via env vars."""
    client = ConnectClient(chalk_client=chalk_client)
    return build_image_with_volumes(client, image, volume_prefix=volume_prefix)


def rpc_error_message(error: ConnectRpcError) -> str:
    """Render a ConnectRPC error as ``CODE: message``."""
    return f"{error.code.name}: {error.message}"


def raise_for_rpc_error(
    error: ConnectRpcError,
    code_map: dict[Code, tuple[type[Exception], str]],
) -> None:
    """Translate a ConnectRPC error to a typed SDK exception when mapped."""
    mapped = code_map.get(error.code)
    if mapped is not None:
        exc_cls, message = mapped
        raise exc_cls(message) from error


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


def rpc_address_from_target(target: str, *, use_tls: bool) -> str:
    """Return an HTTP-style address accepted by ConnectRPC clients."""
    if target.startswith(("http://", "https://")):
        return target.rstrip("/")
    scheme = "https" if use_tls else "http"
    return f"{scheme}://{target.rstrip('/')}"


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
