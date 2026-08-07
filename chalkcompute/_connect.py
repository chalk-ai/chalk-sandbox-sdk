"""Shared Connect-protocol client for Chalk API services.

Provides auth (OAuth token exchange), Connect-protocol RPC dispatch,
and image build polling.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import TYPE_CHECKING, Any, Callable, Mapping, NamedTuple, Sequence, TypeVar
from urllib.parse import urlparse

import grpc

from chalkcompute._auth_config import active_environment_from_env, load_token
from chalkcompute._remote_call_client import (
    RemoteCallClientUnavailable,
    ServiceAuthContext,
    resolve_service_auth_context,
)
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


class _CachedStub(NamedTuple):
    """A memoized gRPC stub plus the channel it was built on.

    The channel is kept so ``ConnectClient.stub`` can detect when the shared
    registry handed back a different channel (e.g. after a fork) and rebuild.
    """

    channel: grpc.Channel
    stub: Any

if TYPE_CHECKING:
    import httpx

    from chalkcompute._image import Image
    from chalkcompute._volume import VolumeMount


class ConnectError(Exception):
    """Raised by ConnectClient operations."""


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
    """Authenticated Connect-protocol (HTTP/JSON) client for Chalk APIs.

    Ambient Chalk credentials are read lazily. A JWT configured through
    ``CHALK_WEB_IDENTITY_TOKEN_FILE`` takes precedence and is cached for the
    shorter of one hour or half its remaining lifetime. Otherwise, service
    auth or ChalkPy's client credentials and ``~/.chalk.yml`` lookup are used.

    Examples
    --------
    >>> client = ConnectClient()
    >>> resp = client.rpc(
    ...     "chalk.container.v1.ContainerService",
    ...     "GetContainer",
    ...     {"id": "..."},
    ... )
    """

    def __init__(self, chalk_client: Any | None = None) -> None:
        self._api_server: str | None = None
        self._access_token: str | None = None
        self._env_id: str | None = None
        self._headers: dict[str, str] | None = None
        self._grpc_engines: dict[str, str] = {}
        self._environment_id_to_name: dict[str, str] = {}
        self._primary_environment: str | None = None
        self._chalk_client = chalk_client
        self._web_identity_token_file: str | None = None
        self._web_identity_cached_at: float | None = None
        self._web_identity_cache_ttl_seconds = _WEB_IDENTITY_MAX_CACHE_TTL_SECONDS
        self._stubs: dict[Any, _CachedStub] = {}

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
            self._env_id = requested_env or identity.environment_id
            self._headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {identity.value}",
                "X-Chalk-Env-Id": self._env_id,
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
            return

        import httpx

        service_auth = service_auth_context_from_env()
        if service_auth is not None:
            self._api_server = service_auth.auth_endpoint.rstrip("/")
            resp = httpx.post(
                f"{self._api_server}/chalk.server.v1.AuthService/GetInternalWorkingToken",
                json={
                    "requestedPermissions": list(service_auth.requested_permissions),
                },
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {service_auth.exchange_token}",
                    "Content-Type": "application/json",
                    "X-Chalk-Server": "go-api",
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            self._access_token = data.get("accessToken") or data["access_token"]
            self._grpc_engines = _string_map(
                data.get("grpcEngines")
                or data.get("grpc_engines")
                or data.get("engines"),
            )
            self._environment_id_to_name = _string_map(
                data.get("environment_id_to_name") or data.get("environmentIdToName"),
            )
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
        self._grpc_engines = _string_map(
            data.get("grpc_engines") or data.get("engines"),
        )
        self._environment_id_to_name = _string_map(data.get("environment_id_to_name"))
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

        if web_identity_token_file_from_env() is not None:
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

    def _grpc_metadata(self) -> list[tuple[str, str]]:
        """Return current gRPC metadata, re-reading rotating identity files."""
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
        if not isinstance(audience, str) or not audience.strip():
            raise ValueError("audience cannot be empty")

        response = self.rpc(
            "chalk.server.v1.AuthService",
            "GetWorkloadIdentityToken",
            {"audience": audience},
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise ConnectError(
                "GetWorkloadIdentityToken returned a response without a token"
            )
        return token

    def stub(self, stub_cls: Callable[[grpc.Channel], _StubT]) -> _StubT:
        """Return a gRPC stub bound to the shared api-server channel.

        Memoized per stub class; rebuilt automatically if the shared channel
        registry handed back a new channel (e.g. after a fork). All stubs on
        one target multiplex over a single HTTP/2 connection.
        """
        from chalkcompute._channel import get_channel

        self._ensure_auth()
        assert self._api_server is not None
        target, use_tls = grpc_target_from_url(self._api_server)
        channel = get_channel(target, use_tls)
        cached = self._stubs.get(stub_cls)
        if cached is not None and cached.channel is channel:
            return cached.stub
        stub = stub_cls(channel)
        self._stubs[stub_cls] = _CachedStub(channel=channel, stub=stub)
        return stub

    def call(
        self,
        method: Callable[..., _RespT],
        request: Any,
        *,
        timeout: float = 30.0,
        extra_metadata: Sequence[tuple[str, str]] | None = None,
    ) -> _RespT:
        """Invoke a unary stub method with per-call auth metadata.

        Metadata is rebuilt on every call via :meth:`_grpc_metadata`, so
        rotating web-identity tokens are picked up without any caller effort.

        Parameters
        ----------
        method
            A bound stub method, e.g. ``client.stub(FooStub).GetFoo``.
        request
            The request protobuf message.
        timeout
            gRPC deadline in seconds.
        extra_metadata
            Additional per-call metadata pairs; keys are lowercased (gRPC
            requires lowercase ASCII keys).

        Raises
        ------
        grpc.RpcError
            On any non-OK status; callers translate to typed errors.
        """
        metadata = self._grpc_metadata()
        if extra_metadata:
            metadata = [*metadata, *((k.lower(), v) for k, v in extra_metadata)]
        return method(request, timeout=timeout, metadata=metadata)

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

        from chalkcompute._tracing import get_tracer

        with get_tracer().start_as_current_span("chalkcompute.rpc") as span:
            span.set_attribute("rpc.service", service)
            span.set_attribute("rpc.method", method)
            self._ensure_auth()
            assert self._api_server is not None
            assert self._headers is not None
            headers = self._headers
            if extra_headers:
                headers = {**headers, **extra_headers}
            resp = httpx.post(
                f"{self._api_server}/{service}/{method}",
                json=body,
                headers=headers,
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


POLL_MAX_ATTEMPTS = 5
"""Total attempts per poll RPC before a transient failure is raised."""


def rpc_with_retries(
    client: ConnectClient,
    service: str,
    method: str,
    body: dict[str, Any],
    *,
    error_cls: type[Exception] = ConnectError,
    max_attempts: int = POLL_MAX_ATTEMPTS,
    retry_interval: float = 1.0,
    timeout: float = 30,
) -> httpx.Response:
    """Call ``client.rpc``, retrying transient failures.

    Status polls should temporarily survive transient failures
    (``httpx.RequestError``, HTTP 429, or a 5xx response). Deterministic
    client errors (other 4xx, e.g. 400/404) would fail identically on every
    attempt, so they raise ``error_cls`` immediately. Each RPC call gets
    ``max_attempts`` total attempts with ``retry_interval`` seconds between
    them; ``error_cls`` is raised only when all attempts fail.

    Returns
    -------
    httpx.Response
        The first response with status 200.
    """
    import httpx

    last_detail = ""
    for attempt in range(1, max_attempts + 1):
        try:
            resp = client.rpc(service, method, body, timeout=timeout)
        except httpx.RequestError as e:
            last_detail = f"{type(e).__name__}: {e}"
        else:
            if resp.status_code == 200:
                return resp
            last_detail = f"HTTP {resp.status_code}: {resp.text}"
            if resp.status_code != 429 and resp.status_code < 500:
                raise error_cls(f"{method} failed: {last_detail}")
        if attempt < max_attempts:
            logger.warning(
                "%s attempt %d/%d failed (%s); retrying in %.1fs",
                method,
                attempt,
                max_attempts,
                last_detail,
                retry_interval,
            )
            time.sleep(retry_interval)
    raise error_cls(f"{method} failed after {max_attempts} attempts: {last_detail}")


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


def grpc_error_message(e: grpc.RpcError) -> str:
    """Render a gRPC error as ``CODE: details`` for exception messages."""
    try:
        assert isinstance(e, grpc.Call)
        return f"{e.code().name}: {e.details()}"
    except Exception:  # noqa: BLE001 - defensive: fakes may not implement Call
        return str(e)


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
