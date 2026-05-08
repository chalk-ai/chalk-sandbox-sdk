"""Shared Connect-protocol client for Chalk API services.

Provides auth (OAuth token exchange), Connect-protocol RPC dispatch,
and image build polling.
"""

from __future__ import annotations

import ast
import functools
import logging
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import grpc
from connectrpc.client import ConnectClientSync as _ConnectClientSync
from connectrpc.code import Code as _ConnectCode
from connectrpc.errors import ConnectError as _ConnectRpcError
from connectrpc.method import IdempotencyLevel, MethodInfo
from google.protobuf import (
    any_pb2,
    descriptor_pb2,
    descriptor_pool,
    duration_pb2,
    empty_pb2,
    field_mask_pb2,
    message_factory,
    struct_pb2,
    timestamp_pb2,
)
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.message import Message

from chalkcompute._tracing import traced as _traced

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from chalkcompute._image import Image


class ConnectError(Exception):
    """Raised by ConnectClient operations."""


class _ConnectCallError(ConnectError):
    def __init__(self, code: _ConnectCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ConnectResponse:
    """Small response shim for existing SDK code that consumes ``.json()``."""

    status_code: int
    text: str
    _json: dict[str, Any]

    def json(self) -> dict[str, Any]:
        return self._json


class ConnectClient:
    """Authenticated Connect-protocol client for Chalk APIs.

    Environment variables (``CHALK_API_SERVER``, ``CHALK_CLIENT_ID``,
    ``CHALK_CLIENT_SECRET``, ``CHALK_ENVIRONMENT_ID``) are read lazily on the
    first RPC. RPC payloads are protobuf messages on the wire; ``rpc`` remains
    as a compatibility wrapper for older call sites that still build dicts.

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
        self._connect_client: _ConnectClientSync | None = None
        self._grpc_engines: dict[str, str] = {}
        self._environment_id_to_name: dict[str, str] = {}
        self._primary_environment: str | None = None

    def _ensure_auth(self) -> None:
        if self._access_token is not None:
            return

        self._api_server = os.environ.get("CHALK_API_SERVER", "").rstrip("/")
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

        request_body = json.dumps(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._api_server}/v1/oauth/token",
            data=request_body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Chalk-Server": "go-api",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise ConnectError(f"OAuth token exchange failed: {detail}") from e
        except urllib.error.URLError as e:
            raise ConnectError(f"OAuth token exchange failed: {e}") from e

        self._access_token = payload["access_token"]
        self._primary_environment = payload.get("primary_environment")
        self._grpc_engines = _string_map(payload.get("grpc_engines"))
        self._environment_id_to_name = _string_map(
            payload.get("environment_id_to_name")
        )
        self._env_id = self._resolve_environment_id(
            requested_env or self._primary_environment or ""
        )

        self._headers = {
            "Authorization": f"Bearer {self._access_token}",
            "X-Chalk-Env-Id": self._env_id or "",
            "X-Chalk-Server": "go-api",
        }
        self._connect_client = _ConnectClientSync(
            self._api_server,
            send_compression=None,
        )

    def unary(
        self,
        service: str,
        method: str,
        request: Message,
        timeout: float = 30,
    ) -> Message:
        """Make a typed unary Connect RPC call."""
        from chalkcompute._tracing import get_tracer

        with get_tracer().start_as_current_span("chalkcompute.rpc") as span:
            span.set_attribute("rpc.service", service)
            span.set_attribute("rpc.method", method)
            self._ensure_auth()
            assert self._headers is not None
            assert self._connect_client is not None
            method_info = _method_info(service, method)
            try:
                response = self._connect_client.execute_unary(
                    request=request,
                    method=method_info,
                    headers=self._headers,
                    timeout_ms=int(timeout * 1000),
                )
            except _ConnectRpcError as e:
                from opentelemetry.trace import StatusCode

                span.set_status(StatusCode.ERROR, e.message)
                raise _ConnectCallError(e.code, e.message) from e
            span.set_attribute("http.response.status_code", 200)
            return response

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
        body: dict[str, Any] | Message,
        timeout: float = 30,
    ) -> ConnectResponse:
        """Make a unary Connect RPC call.

        Parameters
        ----------
        service
            Fully-qualified service name (e.g. ``chalk.container.v1.ContainerService``).
        method
            Method name on the service.
        body
            JSON-shaped dict or generated protobuf request message.
        timeout
            Request timeout in seconds.

        Returns
        -------
        ConnectResponse
            A compatibility response object exposing ``status_code``, ``text``,
            and ``json()``.
        """
        from chalkcompute._tracing import get_tracer

        with get_tracer().start_as_current_span("chalkcompute.rpc") as span:
            span.set_attribute("rpc.service", service)
            span.set_attribute("rpc.method", method)
            try:
                request = (
                    body
                    if isinstance(body, Message)
                    else _parse_request(service, method, body)
                )
                response = self.unary(service, method, request, timeout=timeout)
            except _ConnectCallError as e:
                status_code = _http_status_from_connect_code(e.code)
                span.set_attribute("http.response.status_code", status_code)
                from opentelemetry.trace import StatusCode

                span.set_status(StatusCode.ERROR, e.message)
                return ConnectResponse(
                    status_code=status_code,
                    text=e.message,
                    _json={"code": e.code.value, "message": e.message},
                )
            except ConnectError as e:
                span.set_attribute("http.response.status_code", 500)
                from opentelemetry.trace import StatusCode

                span.set_status(StatusCode.ERROR, str(e))
                return ConnectResponse(status_code=500, text=str(e), _json={})

            data = MessageToDict(response)
            span.set_attribute("http.response.status_code", 200)
            return ConnectResponse(status_code=200, text=json.dumps(data), _json=data)


_GEN_ROOT = Path(__file__).with_name("_gen")
_SERVICE_PROTO_FILES = {
    "chalk.sandbox.v1.SandboxService": "chalk/sandbox/v1/service.proto",
    "chalk.sandbox.v1.CustomImageService": "chalk/sandbox/v1/service.proto",
    "chalk.container.v1.ContainerService": "chalk/container/v1/service.proto",
    "chalk.scalinggroup.v1.ScalingGroupManagerService": (
        "chalk/scalinggroup/v1/service.proto"
    ),
    "chalk.volume.v1.VolumeService": "chalk/volume/v1/volume.proto",
    "chalk.externalfunctioncatalog.v1.ExternalFunctionCatalogService": (
        "chalk/externalfunctioncatalog/v1/service.proto"
    ),
    "chalk.server.v1.EnvironmentSecretsService": (
        "chalk/server/v1/environment_secrets.proto"
    ),
}


def _local_pb2_path(proto_path: str) -> Path:
    if proto_path == "chalk/volume/v1/volume.proto":
        return _GEN_ROOT / "chalk/volume/v1/volume_pb2.py"
    return _GEN_ROOT / proto_path.replace(".proto", "_pb2.py")


def _external_pb2_candidates(proto_path: str) -> tuple[str, ...]:
    module_suffix = proto_path.replace("/", ".").replace(".proto", "_pb2")
    return (
        f"chalk_remote_call._gen.{module_suffix}",
        f"chalk._gen.{module_suffix}",
    )


def _extract_serialized_descriptor(path: Path) -> bytes | None:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "AddSerializedFile"
            and node.args
        ):
            value = ast.literal_eval(node.args[0])
            if isinstance(value, bytes):
                return value
    return None


def _serialized_descriptor(proto_path: str) -> bytes:
    local_path = _local_pb2_path(proto_path)
    if local_path.exists():
        data = _extract_serialized_descriptor(local_path)
        if data is not None:
            return data

    import importlib.util

    for module_name in _external_pb2_candidates(proto_path):
        try:
            spec = importlib.util.find_spec(module_name)
        except ModuleNotFoundError:
            continue
        if spec is None or spec.origin is None:
            continue
        data = _extract_serialized_descriptor(Path(spec.origin))
        if data is not None:
            return data

    raise ConnectError(f"No serialized protobuf descriptor for {proto_path!r}")


@functools.lru_cache(maxsize=1)
def _descriptor_pool() -> descriptor_pool.DescriptorPool:
    """Build a private pool so Connect calls do not conflict with chalkpy stubs."""
    pool = descriptor_pool.DescriptorPool()  # ty: ignore[possibly-missing-implicit-call]
    for descriptor in (
        any_pb2.DESCRIPTOR,
        descriptor_pb2.DESCRIPTOR,
        duration_pb2.DESCRIPTOR,
        empty_pb2.DESCRIPTOR,
        field_mask_pb2.DESCRIPTOR,
        struct_pb2.DESCRIPTOR,
        timestamp_pb2.DESCRIPTOR,
    ):
        pool.AddSerializedFile(descriptor.serialized_pb)

    for proto_path in (
        "buf/validate/validate.proto",
        "chalk/utils/v1/encoding.proto",
        "chalk/auth/v1/permissions.proto",
        "chalk/arrow/v1/arrow.proto",
        "chalk/common/v1/chalk_error.proto",
        "chalk/runtime/v1/remote_python_call.proto",
        "chalk/sandbox/v1/service.proto",
        "chalk/container/v1/service.proto",
        "chalk/scalinggroup/v1/service.proto",
        "chalk/volume/v1/volume.proto",
        "chalk/externalfunctioncatalog/v1/service.proto",
        "chalk/server/v1/environment_secrets.proto",
    ):
        path = _local_pb2_path(proto_path)
        if path.exists() or proto_path in {
            "chalk/auth/v1/permissions.proto",
            "chalk/runtime/v1/remote_python_call.proto",
        }:
            pool.AddSerializedFile(_serialized_descriptor(proto_path))
    return pool


def _message_class(full_name: str) -> Any:
    descriptor = _descriptor_pool().FindMessageTypeByName(full_name)
    return message_factory.GetMessageClass(descriptor)


def _new_message(full_name: str) -> Any:
    return _message_class(full_name)()


def _enum_value(full_name: str, name: str) -> int:
    enum = _descriptor_pool().FindEnumTypeByName(full_name)
    return enum.values_by_name[name].number


def _method_info(service: str, method: str) -> MethodInfo[Any, Any]:
    proto_path = _SERVICE_PROTO_FILES.get(service)
    if proto_path is None:
        raise ConnectError(f"No generated protobuf module for service {service!r}")
    service_name = service.rsplit(".", 1)[1]
    try:
        service_descriptor = _descriptor_pool().FindFileByName(
            proto_path
        ).services_by_name[service_name]
        method_descriptor = service_descriptor.methods_by_name[method]
    except KeyError as e:
        raise ConnectError(f"No generated method {service}/{method}") from e

    request_type = _message_class(method_descriptor.input_type.full_name)
    response_type = _message_class(method_descriptor.output_type.full_name)
    return MethodInfo(
        name=method,
        service_name=service,
        input=request_type,
        output=response_type,
        idempotency_level=IdempotencyLevel.UNKNOWN,
    )


def _parse_request(service: str, method: str, body: dict[str, Any]) -> Message:
    request = _method_info(service, method).input()
    return ParseDict(body, request)


def _http_status_from_connect_code(code: _ConnectCode) -> int:
    return {
        _ConnectCode.CANCELED: 499,
        _ConnectCode.INVALID_ARGUMENT: 400,
        _ConnectCode.DEADLINE_EXCEEDED: 504,
        _ConnectCode.NOT_FOUND: 404,
        _ConnectCode.ALREADY_EXISTS: 409,
        _ConnectCode.PERMISSION_DENIED: 403,
        _ConnectCode.RESOURCE_EXHAUSTED: 429,
        _ConnectCode.FAILED_PRECONDITION: 400,
        _ConnectCode.ABORTED: 409,
        _ConnectCode.OUT_OF_RANGE: 400,
        _ConnectCode.UNIMPLEMENTED: 501,
        _ConnectCode.UNAVAILABLE: 503,
        _ConnectCode.DATA_LOSS: 500,
        _ConnectCode.UNAUTHENTICATED: 401,
    }.get(code, 500)


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
