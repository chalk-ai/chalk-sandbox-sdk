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
from typing import TYPE_CHECKING, Any, Callable, Mapping, TypeVar
from urllib.parse import urlparse

from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError
from connectrpc.protocol import ProtocolType

from chalkcompute._auth_config import active_environment_from_env, load_token
from chalkcompute._gen.chalk.auth.v1 import permissions_pb
from chalkcompute._gen.chalk.sandbox.v1 import service_connect as image_connect
from chalkcompute._gen.chalk.sandbox.v1 import service_pb as image_pb
from chalkcompute._gen.chalk.server.v1 import (
    auth_connect,
    auth_pb,
    team_connect,
    team_pb,
)
from chalkcompute._http import HTTP_TRANSPORT_ERRORS, SYNC_HTTP_CLIENT
from chalkcompute._progress import (
    progress_add,
    progress_done,
    progress_fail,
    progress_finish,
    progress_update,
)
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


class ImageBuildTimeoutError(ConnectError):
    """Raised when the client stops waiting before an image build completes."""

    def __init__(self, build_id: str, timeout: float) -> None:
        self.build_id = build_id
        self.timeout = timeout
        super().__init__(
            f"Image build {build_id} did not complete within the client wait "
            f"timeout of {timeout}s. The build may still be running and was not "
            "cancelled; repeating the same image request can rejoin the build."
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
        self._api_server: str | None = None
        self._access_token: str | None = None
        self._env_id: str | None = None
        self._headers: dict[str, str] | None = None
        self._grpc_engines: dict[str, str] = {}
        self._environment_id_to_name: dict[str, str] = {}
        self._environment_id_to_authority: dict[str, str] = {}
        self._service_prefixes: dict[int, str] = {}
        self._primary_environment: str | None = None
        self._chalk_client = chalk_client
        self._typed_clients: dict[Callable[..., Any], Any] = {}
        self._web_identity_token_file: str | None = None
        self._web_identity_cached_at: float | None = None
        self._web_identity_cache_ttl_seconds = _WEB_IDENTITY_MAX_CACHE_TTL_SECONDS

    @property
    def api_server(self) -> str:
        """The API URL selected by ambient credentials or the supplied ChalkClient.

        Authentication is resolved lazily. Reading this property does not modify
        process environment variables.
        """
        self._ensure_auth()
        assert self._api_server is not None
        return self._api_server.rstrip("/")

    @property
    def environment_id(self) -> str:
        """The resolved environment ID, or an empty string if none was selected."""
        self._ensure_auth()
        return self._env_id or ""

    def request_headers(self) -> dict[str, str]:
        """Return a fresh copy of authentication headers for a Chalk API request.

        Call for each request so rotating workload identity is refreshed. These
        headers contain a bearer credential; do not log them or send them to
        another host.
        """
        return dict(self._rpc_metadata())

    def __enter__(self) -> ConnectClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _ensure_auth(self) -> None:
        identity_token_file = web_identity_token_file_from_env()
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
                "X-Chalk-Env-Id": self._env_id,
                "X-Chalk-Server": "go-api",
            }
            self._grpc_engines = {}
            self._environment_id_to_name = {}
            self._environment_id_to_authority = {}
            self._service_prefixes = {}
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
            return

        service_auth = service_auth_context_from_env()
        if service_auth is not None:
            self._api_server = service_auth.auth_endpoint.rstrip("/")
            auth_client = auth_connect.AuthServiceClientSync(
                self._api_server,
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
            self._access_token = data.access_token
            self._grpc_engines = _string_map(data.grpc_engines or data.engines)
            self._environment_id_to_name = _string_map(data.environment_id_to_name)
            self._environment_id_to_authority = _string_map(
                data.environment_id_to_authority
            )
            self._service_prefixes = _int_string_map(data.service_prefixes)
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
            # Resolve names using login's environment metadata before restricting
            # the token. Keep the scoped response, including its service routes,
            # instead of passing an unscoped token on subsequent API requests.
            self._environment_id_to_name = _string_map(data.environment_id_to_name)
            environment_id = self._resolve_environment_id(
                requested_env or data.primary_environment or ""
            )
            if environment_id:
                data = auth_client.get_token(
                    auth_pb.GetTokenRequest(
                        client_id=client_id,
                        client_secret=client_secret,
                        grant_type="client_credentials",
                        scope=f"env:{environment_id}",
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
        self._environment_id_to_authority = _string_map(
            data.environment_id_to_authority
        )
        self._service_prefixes = _int_string_map(data.service_prefixes)
        self._env_id = environment_id

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
            or environment in self._environment_id_to_authority
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

        if web_identity_token_file_from_env() is not None:
            query_server = os.environ.get("CHALK_GRPC_ENGINE", "").strip()
            if query_server:
                return grpc_engine_target_from_url(query_server)
            query_server = self._grpc_engines.get(self._env_id)
            if not query_server:
                stub = self.stub(team_connect.TeamServiceClientSync)
                try:
                    response = self.call(stub.get_env, team_pb.GetEnvRequest())
                except (ConnectRpcError, *HTTP_TRANSPORT_ERRORS) as exc:
                    raise ConnectError(
                        "Unable to discover the gRPC engine endpoint for Chalk "
                        f"environment {self._env_id!r}: {exc}. Discovery requires "
                        "DEPLOY_READ permission; set CHALK_GRPC_ENGINE to override "
                        "endpoint discovery."
                    ) from exc
                environment = response.environment
                if environment is None or environment.id != self._env_id:
                    raise ConnectError(
                        "Endpoint discovery returned no matching Chalk environment "
                        f"for {self._env_id!r}"
                    )
                query_server = (environment.grpc_engine_url or "").strip()
                if not query_server:
                    raise ConnectError(
                        "No gRPC engine endpoint found for Chalk environment "
                        f"{self._env_id!r}; set CHALK_GRPC_ENGINE to override "
                        "endpoint discovery."
                    )
                # Identity refresh clears this cache, so routing is rediscovered
                # with the current token and environment after rotation.
                self._grpc_engines[self._env_id] = query_server
            return grpc_engine_target_from_url(query_server)

        query_server = self._grpc_engines.get(self._env_id)
        if not query_server:
            available = ", ".join(sorted(self._grpc_engines)) or "none"
            raise ConnectError(
                "No gRPC engine endpoint found for Chalk environment "
                f"{self._env_id!r}. Available environments: {available}"
            )

        return grpc_engine_target_from_url(query_server)

    def dataplane_api_server_target(self) -> tuple[str, bool]:
        """Return the injected workload target or auth's dataplane service URI."""
        self._ensure_auth()
        if not self._env_id:
            raise ConnectError("No Chalk environment selected")
        if self._uses_web_identity_token():
            endpoint_variable = (
                "CHALK_FUNCTION_QUEUE_ENDPOINT"
                if "CHALK_FUNCTION_QUEUE_ENDPOINT" in os.environ
                else "CHALK_DATAPLANE_API_ENDPOINT"
            )
            endpoint = os.environ.get(endpoint_variable, "").strip()
            if not endpoint:
                raise ConnectError(
                    "Queued function calls with CHALK_WEB_IDENTITY_TOKEN_FILE require "
                    f"{endpoint_variable}. The platform must inject the "
                    "dataplane API server endpoint alongside the token file."
                )
            try:
                parsed = urlparse(endpoint if "://" in endpoint else f"//{endpoint}")
                valid = (
                    parsed.scheme in ("", "http", "https")
                    and parsed.hostname
                    and parsed.port != 0
                    and parsed.username is None
                    and parsed.password is None
                    and parsed.path in ("", "/")
                    and "?" not in endpoint
                    and "#" not in endpoint
                    and not parsed.netloc.endswith(":")
                    and not any(char.isspace() for char in endpoint)
                )
            except ValueError:
                valid = False
            if not valid:
                raise ConnectError(
                    f"Invalid {endpoint_variable}: expected a dataplane "
                    "API server host with optional port and http:// or https:// scheme"
                )
            return grpc_engine_target_from_url(
                parsed.geturl() if parsed.scheme else endpoint
            )
        authority = self._environment_id_to_authority.get(self._env_id, "")
        service_prefix = self._service_prefixes.get(
            auth_pb.ServiceKind.DATAPLANE_API_SERVER.value, ""
        )
        if not authority or not service_prefix:
            raise ConnectError(
                "No dataplane API server endpoint found for Chalk environment "
                f"{self._env_id!r}; auth did not provide service URI metadata"
            )
        return grpc_engine_target_from_url(f"https://{service_prefix}.{authority}")

    def _rpc_metadata(self) -> list[tuple[str, str]]:
        """Return current RPC metadata, re-reading rotating identity files."""
        self._ensure_auth()
        return [
            ("authorization", f"Bearer {self._access_token}"),
            ("x-chalk-env-id", self._env_id or ""),
            ("x-chalk-server", "go-api"),
        ]

    def _uses_web_identity_token(self) -> bool:
        """Return whether rotating web-identity authentication is configured."""
        return web_identity_token_file_from_env() is not None

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
        response = self._get_workload_identity_token_response(
            audience,
            timeout=timeout,
        )
        return response.token

    def _get_workload_identity_token_response(
        self,
        audience: str,
        *,
        timeout: float = 30,
    ) -> auth_pb.GetWorkloadIdentityTokenResponse:
        """Return the complete workload identity token RPC response."""
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
        return response

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
    timeout: float | None = None,
    progress_parent: str | None = None,
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
        Optional maximum seconds to wait for the build to complete. By default,
        wait for the server to report a terminal build status.
    progress_parent
        Optional progress row to nest the "Building image" step under.

    Returns
    -------
    str
        The built image URI.

    Raises
    ------
    ConnectError
        If the build fails or an explicitly configured timeout expires.
    """
    image_spec = image.to_proto()
    image_uri = image.as_image_uri_if_possible()
    if image_uri is not None:
        return image_uri

    stub = client.stub(image_connect.CustomImageServiceClientSync)
    response = client.call(
        stub.get_or_build_custom_image,
        image_pb.GetOrBuildCustomImageRequest(image_spec=image_spec),
    )
    status = response.status
    if status in ("exists", "succeeded"):
        return response.image
    if status == "failed":
        raise ConnectError(f"Image build failed: {response.error or 'unknown'}")

    return wait_for_image_build(
        client,
        response.build_id,
        poll_interval=poll_interval,
        timeout=timeout,
        progress_parent=progress_parent,
    )


def wait_for_image_build(
    client: ConnectClient,
    build_id: str,
    *,
    poll_interval: float,
    timeout: float | None,
    progress_parent: str | None = None,
) -> str:
    """Wait for an already-submitted build without another get-or-build call.

    Shows a "Building image" row in the deploy progress display while waiting,
    nested under ``progress_parent`` when the caller has its own progress row.
    """
    key = f"image-build:{build_id}"
    started = time.monotonic()
    progress_add(
        key,
        "image",
        "Building image",
        parent=progress_parent,
        detail=f"Build started ({build_id})",
    )
    try:
        image = _poll_image_build(
            client, build_id, key, poll_interval=poll_interval, timeout=timeout
        )
    except BaseException:
        progress_fail(key, detail="build did not complete")
        if progress_parent is None:
            progress_finish()
        raise
    progress_done(key, detail=f"built in {time.monotonic() - started:.0f}s")
    if progress_parent is None:
        progress_finish()
    return image


def _poll_image_build(
    client: ConnectClient,
    build_id: str,
    progress_key: str,
    *,
    poll_interval: float,
    timeout: float | None,
) -> str:
    stub = client.stub(image_connect.CustomImageServiceClientSync)
    deadline = time.monotonic() + timeout if timeout is not None else None
    transient_failures = 0
    max_transient_failures = 5
    announced_building = False
    while deadline is None or time.monotonic() < deadline:
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
        if status == "building" and not announced_building:
            announced_building = True
            progress_update(progress_key, detail="Building")
        if status == "succeeded":
            return get_response.image
        if status == "failed":
            raise ConnectError(
                f"Image build {build_id} failed: {get_response.error or 'unknown'}"
            )
        time.sleep(poll_interval)

    assert timeout is not None
    raise ImageBuildTimeoutError(build_id, timeout)


def build_image_from_spec(image: Image, chalk_client: Any | None = None) -> str:
    """Build an image and return its URI.

    Parameters
    ----------
    image : Image
        Image definition without volume-backed local files.
    chalk_client : Any, optional
        Chalk client supplying authentication. Defaults to environment auth.

    Returns
    -------
    str
        Built or cached image URI.

    Raises
    ------
    ValueError
        If volume-backed files are present. Use ``Image.build()`` or
        ``build_image_with_volumes`` to include them.
    ConnectError
        If the image build fails or times out.
    """
    if image.lazy_local_files or image._volume_mounts:
        raise ValueError(
            "build_image() drops volume-backed local files. "
            "Use Image.build() or build_image_with_volumes()."
        )
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
    from chalkcompute._image import Image
    from chalkcompute._image_build import normalize_image_volumes
    from chalkcompute._volume import VolumeClient

    recipe, volumes = normalize_image_volumes(image, None)
    assert isinstance(recipe, Image)
    image_uri = build_image(
        client,
        recipe,
        poll_interval=poll_interval,
        timeout=timeout,
    )
    if image.lazy_local_files:
        volumes += VolumeClient.from_connect(client).upload_lazy_files(
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
    """Build an image and upload its volume-backed local files.

    Parameters
    ----------
    image : Image
        Image definition and local files to upload.
    volume_prefix : str, optional
        Prefix for generated volume names. Defaults to ``"files"``.
    chalk_client : Any, optional
        Chalk client supplying authentication. Defaults to environment auth.

    Returns
    -------
    tuple of str and list of VolumeMount
        Image URI and pinned mounts for the image content.

    Raises
    ------
    ConnectError
        If the image build fails or times out.
    VolumeError
        If local file validation or upload fails.
    """
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


def _int_string_map(value: Any) -> dict[int, str]:
    if not isinstance(value, dict):
        return {}
    return {int(k): str(v) for k, v in value.items()}
