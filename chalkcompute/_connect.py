"""Shared authenticated client for Chalk API services.

Provides auth (OAuth token exchange), typed RPC dispatch through ConnectRPC
clients via ``stub()``/``call()``, plus the legacy untyped Connect JSON
``rpc()`` helper and image build polling.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol, TypeVar
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
    WEB_IDENTITY_TOKEN_FILE_ENV_VAR,
    WebIdentityTokenError,
    read_web_identity_token,
    web_identity_token_file_from_env,
)

logger = logging.getLogger(__name__)

_WEB_IDENTITY_MAX_REFRESH_INTERVAL_SECONDS = 60 * 60
_WEB_IDENTITY_EXPIRY_WINDOW_SECONDS = 5 * 60
_WEB_IDENTITY_MIN_REFRESH_INTERVAL_SECONDS = 5.0

_StubT = TypeVar("_StubT")
_RespT = TypeVar("_RespT")


if TYPE_CHECKING:
    from chalkcompute._image import Image
    from chalkcompute._volume import VolumeMount


class ConnectError(Exception):
    """Raised by ConnectClient operations."""


@dataclass(frozen=True)
class _AuthState:
    """Immutable credentials and routing data for one authenticated request."""

    api_server: str
    access_token: str
    environment_id: str
    grpc_engines: Mapping[str, str]
    environment_id_to_name: Mapping[str, str]
    primary_environment: str | None = None


class _AuthProvider(Protocol):
    """Source of current credentials for a configured authentication mode."""

    uses_web_identity: bool

    def state(self) -> _AuthState: ...


class _StaticAuthProvider:
    """Load non-rotating credentials once, on their first use."""

    uses_web_identity = False

    def __init__(self, load: Callable[[], _AuthState]) -> None:
        self._load = load
        self._state: _AuthState | None = None

    def state(self) -> _AuthState:
        if self._state is None:
            self._state = self._load()
        return self._state


class _WebIdentityAuthProvider:
    """Read a rotating identity JWT without rediscovering client configuration."""

    uses_web_identity = True

    def __init__(
        self,
        *,
        token_file: str,
        api_server: str,
        requested_environment: str | None,
    ) -> None:
        self._token_file = token_file
        self._api_server = api_server
        self._requested_environment = requested_environment
        self._state: _AuthState | None = None
        self._refresh_after: float | None = None
        self._error_message: str | None = None

    def state(self) -> _AuthState:
        monotonic_now = time.monotonic()
        if self._refresh_after is not None and monotonic_now < self._refresh_after:
            if self._error_message is not None:
                raise WebIdentityTokenError(self._error_message)
            assert self._state is not None
            return self._state

        try:
            identity = read_web_identity_token(self._token_file)
        except WebIdentityTokenError as exc:
            self._cache_error(str(exc), monotonic_now=monotonic_now)
            raise
        wall_now = time.time()
        if identity.expires_at is not None and identity.expires_at <= wall_now:
            error_message = (
                f"JWT in {WEB_IDENTITY_TOKEN_FILE_ENV_VAR} file "
                f"{self._token_file!r} has expired"
            )
            self._cache_error(error_message, monotonic_now=monotonic_now)
            raise WebIdentityTokenError(error_message)

        self._error_message = None
        self._state = _AuthState(
            api_server=self._api_server,
            access_token=identity.value,
            environment_id=(
                self._requested_environment or identity.environment_id or ""
            ),
            grpc_engines=MappingProxyType({}),
            environment_id_to_name=MappingProxyType({}),
        )
        self._refresh_after = monotonic_now + _web_identity_refresh_interval(
            expires_at=identity.expires_at,
            wall_now=wall_now,
        )
        return self._state

    def _cache_error(
        self,
        error_message: str,
        *,
        monotonic_now: float,
    ) -> None:
        self._error_message = error_message
        self._refresh_after = (
            monotonic_now + _WEB_IDENTITY_MIN_REFRESH_INTERVAL_SECONDS
        )


def _web_identity_refresh_interval(
    *,
    expires_at: float | None,
    wall_now: float,
) -> float:
    """Return a bounded refresh interval that does not collapse near expiry."""
    if expires_at is None:
        return _WEB_IDENTITY_MAX_REFRESH_INTERVAL_SECONDS
    return min(
        _WEB_IDENTITY_MAX_REFRESH_INTERVAL_SECONDS,
        max(
            _WEB_IDENTITY_MIN_REFRESH_INTERVAL_SECONDS,
            expires_at - wall_now - _WEB_IDENTITY_EXPIRY_WINDOW_SECONDS,
        ),
    )


def service_auth_context_from_env() -> ServiceAuthContext | None:
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
    from chalk.client import ChalkClient

    headers = additional_headers
    has_authorization = any(key.lower() == "authorization" for key in headers or {})
    # Web-identity rotation is implemented by this SDK's own transports.
    # A ChalkClient built here would cache the injected header indefinitely.
    service_auth = (
        None
        if web_identity_token_file_from_env() is not None
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


class ConnectClient:
    """Authenticated RPC client for Chalk APIs.

    Typed RPC dispatch goes through ConnectRPC clients using the gRPC protocol
    over the shared HTTP/2 transport. Ambient Chalk credentials are read lazily.
    A JWT configured through ``CHALK_WEB_IDENTITY_TOKEN_FILE`` takes precedence
    and is refreshed at most hourly and before its expiration window. Otherwise,
    service auth or ChalkPy's client credentials and ``~/.chalk.yml`` lookup are
    used. Auth mode and endpoint configuration are fixed on first use.

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
        self._api_server: str | None = None
        self._access_token: str | None = None
        self._env_id: str | None = None
        self._grpc_engines: dict[str, str] = {}
        self._environment_id_to_name: dict[str, str] = {}
        self._primary_environment: str | None = None
        self._chalk_client = chalk_client
        self._typed_clients: dict[Callable[..., Any], Any] = {}
        self._auth_lock = threading.RLock()
        self._auth_provider: _AuthProvider | None = None
        self._auth_state_value: _AuthState | None = None

    def _ensure_auth(self) -> None:
        self._auth_state()

    def _auth_state(self) -> _AuthState:
        """Return one internally consistent authentication snapshot."""
        with self._auth_lock:
            # Preserve the existing injection seam where callers populate the
            # private credential fields directly.
            if self._auth_provider is None and self._access_token is not None:
                return self._state_from_fields()

            if self._auth_provider is None:
                self._auth_provider = self._auth_provider_from_env()

            try:
                state = self._auth_provider.state()
            except WebIdentityTokenError as exc:
                raise ConnectError(str(exc)) from exc
            if state is not self._auth_state_value:
                self._apply_auth_state(state)
            return state

    def _auth_provider_from_env(self) -> _AuthProvider:
        """Select and configure an auth provider once for this client."""
        identity_token_file = web_identity_token_file_from_env()
        if identity_token_file is not None:
            provider = _WebIdentityAuthProvider(
                token_file=identity_token_file,
                api_server=(
                    os.environ.get("CHALK_API_SERVER") or "https://api.chalk.ai"
                ).rstrip("/"),
                requested_environment=active_environment_from_env(),
            )
            return provider

        service_auth = service_auth_context_from_env()
        if service_auth is not None:
            return _StaticAuthProvider(
                lambda: self._exchange_service_auth(service_auth)
            )
        return _StaticAuthProvider(self._exchange_client_credentials)

    def _exchange_service_auth(
        self,
        service_auth: ServiceAuthContext,
    ) -> _AuthState:
        api_server = service_auth.auth_endpoint.rstrip("/")
        auth_client = auth_connect.AuthServiceClientSync(
            api_server,
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
        grpc_engines = MappingProxyType(
            _string_map(data.grpc_engines or data.engines)
        )
        environment_id_to_name = MappingProxyType(
            _string_map(data.environment_id_to_name)
        )
        return _AuthState(
            api_server=api_server,
            access_token=data.access_token,
            environment_id=_resolve_environment_id(
                service_auth.environment_id,
                grpc_engines=grpc_engines,
                environment_id_to_name=environment_id_to_name,
            ),
            grpc_engines=grpc_engines,
            environment_id_to_name=environment_id_to_name,
        )

    def _exchange_client_credentials(self) -> _AuthState:
        token = (
            None
            if self._chalk_client is not None
            else load_token(None, None, None, None)
        )
        api_server = (
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
            if not api_server:
                api_server = cc._api_server or ""
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

        if not api_server or not client_id or not client_secret:
            raise ConnectError(
                "No Chalk credentials found. Set CHALK_CLIENT_ID/CHALK_CLIENT_SECRET "
                "environment variables, run `chalk login`, or pass a ChalkClient instance."
            )

        auth_client = auth_connect.AuthServiceClientSync(
            api_server,
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
        primary_environment = data.primary_environment or None
        grpc_engines = MappingProxyType(
            _string_map(data.grpc_engines or data.engines)
        )
        environment_id_to_name = MappingProxyType(
            _string_map(data.environment_id_to_name)
        )
        return _AuthState(
            api_server=api_server,
            access_token=data.access_token,
            environment_id=_resolve_environment_id(
                requested_env or primary_environment or "",
                grpc_engines=grpc_engines,
                environment_id_to_name=environment_id_to_name,
            ),
            grpc_engines=grpc_engines,
            environment_id_to_name=environment_id_to_name,
            primary_environment=primary_environment,
        )

    def _apply_auth_state(self, state: _AuthState) -> None:
        self._auth_state_value = state
        self._api_server = state.api_server
        self._access_token = state.access_token
        self._env_id = state.environment_id
        self._grpc_engines = dict(state.grpc_engines)
        self._environment_id_to_name = dict(state.environment_id_to_name)
        self._primary_environment = state.primary_environment

    def _state_from_fields(self) -> _AuthState:
        return _AuthState(
            api_server=self._api_server or "",
            access_token=self._access_token or "",
            environment_id=self._env_id or "",
            grpc_engines=MappingProxyType(dict(self._grpc_engines)),
            environment_id_to_name=MappingProxyType(
                dict(self._environment_id_to_name)
            ),
            primary_environment=self._primary_environment,
        )

    def grpc_engine_target(self) -> tuple[str, bool]:
        """Return the active environment's gRPC engine target and TLS mode."""
        state = self._auth_state()
        if not state.environment_id:
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

        query_server = state.grpc_engines.get(state.environment_id)
        if not query_server:
            available = ", ".join(sorted(state.grpc_engines)) or "none"
            raise ConnectError(
                "No gRPC engine endpoint found for Chalk environment "
                f"{state.environment_id!r}. Available environments: {available}"
            )

        return grpc_engine_target_from_url(query_server)

    def _rpc_metadata(self) -> list[tuple[str, str]]:
        """Return current RPC metadata, re-reading rotating identity files."""
        state = self._auth_state()
        return [
            ("authorization", f"Bearer {state.access_token}"),
            ("x-chalk-env-id", state.environment_id),
            ("x-chalk-server", "go-api"),
        ]

    def _uses_web_identity_token(self) -> bool:
        """Return whether rotating web-identity authentication is configured."""
        with self._auth_lock:
            if self._auth_provider is None:
                if self._access_token is not None:
                    return False
                self._auth_provider = self._auth_provider_from_env()
            return self._auth_provider.uses_web_identity

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
        with self._auth_lock:
            client = self._typed_clients.get(stub_cls)
            if client is not None:
                return client
            state = self._auth_state()
            client = stub_cls(
                state.api_server,
                protocol=ProtocolType.GRPC,
                interceptors=rpc_interceptors(),
                http_client=SYNC_HTTP_CLIENT,
                read_max_bytes=RPC_READ_MAX_BYTES,
            )
            self._typed_clients[stub_cls] = client
            return client

    def close(self) -> None:
        """Close cached typed ConnectRPC clients."""
        with self._auth_lock:
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

    build_id = response.build_id
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


def _resolve_environment_id(
    environment: str,
    *,
    grpc_engines: Mapping[str, str],
    environment_id_to_name: Mapping[str, str],
) -> str:
    if not environment:
        return ""
    if environment in grpc_engines or environment in environment_id_to_name:
        return environment

    lower_environment = environment.lower()
    matches = [
        env_id
        for env_id, env_name in environment_id_to_name.items()
        if env_name.lower() == lower_environment
    ]
    if len(matches) > 1:
        raise ConnectError(
            f"Multiple Chalk environments match {environment!r}: {matches}"
        )
    if len(matches) == 1:
        return matches[0]
    return environment
