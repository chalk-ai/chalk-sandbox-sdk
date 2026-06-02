"""chalkcompute.mcp.gateway — resolve and adapt MCP tools from a Chalk MCP gateway.

Point the SDK at a `chalk-mcp-gateway
<https://github.com/chalk-ai/chalk-mcp-gateway>`_ instance via the
``CHALK_MCP_GATEWAY_API`` environment variable (and an optional bearer token in
``CHALK_MCP_GATEWAY_TOKEN``), then resolve gateway tools by name and adapt them
into provider-native tool specs.

Examples
--------
>>> from chalkcompute.mcp.gateway import Tool, OpenaiTool
>>>
>>> # Resolve a tool the gateway exposes (gateway- or dotted-form name).
>>> tool = Tool.from_name("linear__create_issue")     # or "linear.create_issue"
>>>
>>> # Adapt it to an OpenAI tool spec (``from`` is a keyword, hence ``from_tool``).
>>> spec = OpenaiTool.from_tool(tool)
>>>
>>> # ... and/or call it directly through the gateway.
>>> result = tool.call(title="flaky CI", teamId="ENG")
>>> print(result.text)
"""

from __future__ import annotations

from chalkcompute.mcp.gateway._adapters import AnthropicTool, OpenaiTool
from chalkcompute.mcp.gateway._client import (
    GatewayClient,
    get_default_client,
    reset_default_client,
)
from chalkcompute.mcp.gateway._errors import (
    GatewayConfigError,
    GatewayError,
    GatewayProtocolError,
    ToolCallError,
    ToolNotFoundError,
)
from chalkcompute.mcp.gateway._tool import Tool, ToolResult

__all__ = [
    "AnthropicTool",
    "GatewayClient",
    "GatewayConfigError",
    "GatewayError",
    "GatewayProtocolError",
    "OpenaiTool",
    "Tool",
    "ToolCallError",
    "ToolNotFoundError",
    "ToolResult",
    "get_default_client",
    "reset_default_client",
]
