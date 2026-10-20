"""Minimal MCP Streamable-HTTP client for the Chalk MCP gateway.

The gateway speaks the MCP *Streamable HTTP* transport: every JSON-RPC message
is ``POST``\\ ed to a single ``/mcp`` endpoint, and the response is either a
JSON body or an ``text/event-stream`` (SSE) body carrying the JSON-RPC reply.
This module implements just enough of that transport — ``initialize``,
``tools/list`` and ``tools/call`` — to resolve and invoke tools.

The endpoint is taken from the ``CHALK_MCP_GATEWAY_API`` environment variable
(or an explicit ``api=`` argument) and the bearer token from
``CHALK_MCP_GATEWAY_TOKEN`` / ``GATEWAY_TOKEN`` (or an explicit ``token=``).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from chalkcompute.mcp.gateway._errors import (
    GatewayConfigError,
    GatewayProtocolError,
)

if TYPE_CHECKING:
    from chalkcompute.mcp.gateway._tool import Tool, ToolResult

logger = logging.getLogger(__name__)

_API_ENV_VAR = "CHALK_MCP_GATEWAY_API"
_TOKEN_ENV_VARS = ("CHALK_MCP_GATEWAY_TOKEN", "GATEWAY_TOKEN")
_MCP_PATH = "/mcp"
_DEFAULT_PROTOCOL_VERSION = "2025-06-18"
_DEFAULT_CLIENT_NAME = "chalkcompute-mcp-gateway"
_CLIENT_VERSION = "0.1.0"
_DEFAULT_TIMEOUT_SECS = 30.0


def _normalize_endpoint(api: str) -> str:
    """Normalize a gateway URI into its ``/mcp`` POST endpoint.

    Accepts either the gateway base URL (``https://gw.example.com``) or the
    fully-qualified endpoint (``https://gw.example.com/mcp``); the ``/mcp``
    suffix is appended when missing. Query strings and fragments are dropped.

    Parameters
    ----------
    api
        The gateway URI.

    Returns
    -------
    str
        The normalized ``POST`` endpoint.

    Raises
    ------
    GatewayConfigError
        If ``api`` is not an absolute ``http(s)`` URL.
    """
    parts = urlsplit(api.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise GatewayConfigError(
            f"Invalid gateway URI {api!r}: expected an absolute http(s) URL "
            f"such as 'https://gateway.example.com' or '.../mcp'."
        )
    path = parts.path.rstrip("/")
    if not path.endswith(_MCP_PATH):
        path = path + _MCP_PATH
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _extract_jsonrpc(resp: httpx.Response) -> dict[str, Any]:
    """Pull the JSON-RPC envelope out of a JSON or SSE gateway response.

    Parameters
    ----------
    resp
        The HTTP response from the gateway.

    Returns
    -------
    dict[str, Any]
        The JSON-RPC envelope (the dict carrying ``result`` or ``error``).

    Raises
    ------
    GatewayProtocolError
        If no JSON-RPC envelope can be parsed from the body.
    """
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for raw in resp.text.splitlines():
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if not data:
                continue
            try:
                val = json.loads(data)
            except ValueError:
                continue
            if isinstance(val, dict) and ("result" in val or "error" in val):
                return val
        raise GatewayProtocolError(
            f"No JSON-RPC response found in SSE stream: {resp.text[:500]!r}"
        )

    try:
        val = resp.json()
    except ValueError as exc:
        raise GatewayProtocolError(
            f"Gateway returned invalid JSON: {resp.text[:500]!r}"
        ) from exc

    if isinstance(val, list):
        for item in val:
            if isinstance(item, dict) and ("result" in item or "error" in item):
                return item
        raise GatewayProtocolError(f"Empty JSON-RPC batch from gateway: {val!r}")
    if not isinstance(val, dict):
        raise GatewayProtocolError(f"Unexpected JSON-RPC payload from gateway: {val!r}")
    return val


class GatewayClient:
    """A session-scoped client for a single Chalk MCP gateway endpoint.

    The MCP session (``initialize`` handshake + ``mcp-session-id``) is
    established lazily on first use and reused for the lifetime of the client.
    Instances are usable as context managers and own an :class:`httpx.Client`.

    Examples
    --------
    >>> with GatewayClient() as gw:          # reads CHALK_MCP_GATEWAY_API
    ...     for tool in gw.list_tools():
    ...         print(tool.name)
    """

    def __init__(
        self,
        api: str | None = None,
        *,
        token: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECS,
        client_name: str = _DEFAULT_CLIENT_NAME,
        protocol_version: str = _DEFAULT_PROTOCOL_VERSION,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Construct a gateway client.

        Parameters
        ----------
        api
            Gateway URI. Defaults to ``$CHALK_MCP_GATEWAY_API``.
        token
            Bearer token. Defaults to ``$CHALK_MCP_GATEWAY_TOKEN`` then
            ``$GATEWAY_TOKEN``. May be ``None`` for an unauthenticated gateway.
        timeout
            Per-request timeout in seconds.
        client_name
            ``clientInfo.name`` reported during the MCP handshake.
        protocol_version
            MCP protocol version offered during ``initialize``. The server's
            negotiated version is adopted if it differs.
        transport
            Optional :class:`httpx.BaseTransport` (used in tests).

        Raises
        ------
        GatewayConfigError
            If no gateway URI can be resolved.
        """
        resolved_api = api or os.environ.get(_API_ENV_VAR)
        if not resolved_api:
            raise GatewayConfigError(
                f"No gateway URI provided. Set ${_API_ENV_VAR} or pass api=..."
            )
        resolved_token = token
        if resolved_token is None:
            for env_var in _TOKEN_ENV_VARS:
                resolved_token = os.environ.get(env_var)
                if resolved_token:
                    break

        self._endpoint = _normalize_endpoint(resolved_api)
        self._token = resolved_token or None
        self._client_name = client_name
        self._protocol_version = protocol_version
        self._http = httpx.Client(timeout=timeout, transport=transport)

        self._lock = threading.Lock()
        self._session_id: str | None = None
        self._initialized = False
        self._id_counter = 0

    # -- public API ---------------------------------------------------------

    @property
    def endpoint(self) -> str:
        """The normalized ``/mcp`` POST endpoint this client targets."""
        return self._endpoint

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a JSON-RPC request (after ensuring the session is initialized).

        Parameters
        ----------
        method
            The JSON-RPC method (e.g. ``"tools/list"``).
        params
            The method params.

        Returns
        -------
        Any
            The JSON-RPC ``result`` payload.
        """
        self._ensure_initialized()
        with self._lock:
            return self._rpc(method, params)

    def list_tool_specs(self) -> list[dict[str, Any]]:
        """Return the raw MCP tool descriptors exposed by the gateway.

        Follows ``nextCursor`` pagination until exhausted.

        Returns
        -------
        list[dict[str, Any]]
            Each dict has ``name``, optional ``description`` and ``inputSchema``.
        """
        specs: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
            result = self.request("tools/list", params)
            if not isinstance(result, dict):
                break
            for tool in result.get("tools", []):
                if isinstance(tool, dict):
                    specs.append(tool)
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
        return specs

    def call_tool_raw(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Invoke a tool and return the raw MCP ``CallToolResult`` dict.

        Parameters
        ----------
        name
            The namespaced tool name (e.g. ``"linear__create_issue"``).
        arguments
            The tool arguments.

        Returns
        -------
        dict[str, Any]
            The MCP ``result`` (``content`` / ``isError`` / ``structuredContent``).

        Raises
        ------
        GatewayProtocolError
            If the gateway returns a non-dict result.
        """
        result = self.request(
            "tools/call", {"name": name, "arguments": arguments or {}}
        )
        if not isinstance(result, dict):
            raise GatewayProtocolError(
                f"tools/call for {name!r} returned a non-object result: {result!r}"
            )
        return result

    def list_tools(self) -> list[Tool]:
        """Return the gateway's tools as resolved :class:`Tool` objects."""
        from chalkcompute.mcp.gateway._tool import Tool

        return [Tool.from_spec(spec, client=self) for spec in self.list_tool_specs()]

    def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> ToolResult:
        """Invoke a tool and return a parsed :class:`ToolResult`."""
        from chalkcompute.mcp.gateway._tool import ToolResult

        return ToolResult._from_result(self.call_tool_raw(name, arguments))

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()

    def __enter__(self) -> GatewayClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals ----------------------------------------------------------

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    def _build_headers(self, *, include_protocol: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        if include_protocol:
            headers["MCP-Protocol-Version"] = self._protocol_version
        return headers

    def _send(
        self,
        payload: dict[str, Any],
        *,
        is_notification: bool = False,
        include_protocol: bool = True,
    ) -> dict[str, Any] | None:
        resp = self._http.post(
            self._endpoint,
            headers=self._build_headers(include_protocol=include_protocol),
            json=payload,
        )
        session_id = resp.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        if resp.status_code >= 400:
            raise GatewayProtocolError(
                f"Gateway returned HTTP {resp.status_code} for "
                f"{payload.get('method')!r}: {resp.text[:500]!r}"
            )
        if is_notification:
            return None
        return _extract_jsonrpc(resp)

    def _check(self, envelope: dict[str, Any] | None) -> Any:
        if not isinstance(envelope, dict):
            raise GatewayProtocolError("Gateway returned an empty JSON-RPC response")
        error = envelope.get("error")
        if error is not None:
            if isinstance(error, dict):
                raise GatewayProtocolError(
                    f"JSON-RPC error {error.get('code')}: {error.get('message')}"
                )
            raise GatewayProtocolError(f"JSON-RPC error: {error!r}")
        if "result" not in envelope:
            raise GatewayProtocolError(
                f"JSON-RPC response missing 'result': {envelope!r}"
            )
        return envelope["result"]

    def _rpc(self, method: str, params: dict[str, Any] | None) -> Any:
        envelope = self._send(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": method,
                "params": params or {},
            }
        )
        return self._check(envelope)

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            envelope = self._send(
                {
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "initialize",
                    "params": {
                        "protocolVersion": self._protocol_version,
                        "capabilities": {},
                        "clientInfo": {
                            "name": self._client_name,
                            "version": _CLIENT_VERSION,
                        },
                    },
                },
                include_protocol=False,
            )
            result = self._check(envelope)
            if isinstance(result, dict):
                server_version = result.get("protocolVersion")
                if isinstance(server_version, str) and server_version:
                    self._protocol_version = server_version
            self._send(
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                is_notification=True,
            )
            self._initialized = True
            logger.debug(
                "Initialized MCP session with %s (protocol %s)",
                self._endpoint,
                self._protocol_version,
            )


_default_lock = threading.Lock()
_default_client: GatewayClient | None = None


def get_default_client() -> GatewayClient:
    """Return a process-wide default client built from the environment.

    The client is created lazily on first call (reading
    ``$CHALK_MCP_GATEWAY_API`` and friends) and reused thereafter so that
    repeated :meth:`Tool.from_name` calls share one MCP session.

    Returns
    -------
    GatewayClient
        The cached default client.

    Raises
    ------
    GatewayConfigError
        If no gateway URI can be resolved from the environment.
    """
    global _default_client
    with _default_lock:
        if _default_client is None:
            _default_client = GatewayClient()
        return _default_client


def reset_default_client() -> None:
    """Drop (and close) the cached default client.

    Useful after changing ``$CHALK_MCP_GATEWAY_API`` or in tests.
    """
    global _default_client
    with _default_lock:
        if _default_client is not None:
            _default_client.close()
        _default_client = None
