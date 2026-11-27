"""Exceptions raised by the chalkcompute MCP gateway client."""

from __future__ import annotations


class GatewayError(Exception):
    """Base class for every error raised by :mod:`chalkcompute.mcp.gateway`."""


class GatewayConfigError(GatewayError):
    """Raised when the gateway endpoint (or token) cannot be resolved.

    Typically this means ``CHALK_MCP_GATEWAY_API`` is unset or malformed and no
    ``api=`` argument was supplied.
    """


class GatewayProtocolError(GatewayError):
    """Raised when the gateway returns a non-2xx HTTP status or a malformed /
    error JSON-RPC response.
    """


class ToolNotFoundError(GatewayError):
    """Raised when a named tool cannot be resolved against the gateway."""

    def __init__(self, name: str, available: list[str] | None = None) -> None:
        self.name = name
        self.available = available or []
        msg = f"No tool named {name!r} is exposed by the gateway."
        if self.available:
            preview = ", ".join(sorted(self.available)[:20])
            suffix = ", …" if len(self.available) > 20 else ""
            msg += f" Available tools: {preview}{suffix}"
        super().__init__(msg)


class ToolCallError(GatewayError):
    """Raised when a ``tools/call`` fails or the tool reports ``isError``.

    The originating :class:`~chalkcompute.mcp.gateway.ToolResult` (when present)
    is available on the ``result`` attribute.
    """

    def __init__(self, message: str, result: object | None = None) -> None:
        super().__init__(message)
        self.result = result
