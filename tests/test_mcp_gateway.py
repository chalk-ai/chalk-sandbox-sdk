"""Unit tests for chalkcompute.mcp.gateway.

These exercise the MCP Streamable-HTTP client, tool resolution, and the
provider adapters against an in-process :class:`httpx.MockTransport` standing in
for a live ``chalk-mcp-gateway``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from chalkcompute.mcp.gateway import (
    AnthropicTool,
    GatewayClient,
    GatewayConfigError,
    GatewayProtocolError,
    OpenaiTool,
    Tool,
    ToolCallError,
    ToolNotFoundError,
)
from chalkcompute.mcp.gateway._client import _normalize_endpoint

# ---------------------------------------------------------------------------
# Fake gateway
# ---------------------------------------------------------------------------

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "gateway__whoami",
        "description": "Return the caller identity.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "linear__create_issue",
        "description": "Create a Linear issue.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "teamId": {"type": "string"},
            },
            "required": ["title"],
        },
    },
]


def _make_handler(
    *, sse: bool = False, calls: list[dict[str, Any]] | None = None
) -> Any:
    """Build an httpx handler that emulates the gateway's MCP endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if calls is not None:
            calls.append({"headers": dict(request.headers), "body": body})
        method = body.get("method")

        if method == "initialize":
            return httpx.Response(
                200,
                headers={"mcp-session-id": "sess-123"},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "chalk-mcp-gateway", "version": "0"},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            payload = {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"tools": _TOOLS},
            }
            if sse:
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    text=f"event: message\ndata: {json.dumps(payload)}\n\n",
                )
            return httpx.Response(200, json=payload)
        if method == "tools/call":
            name = body["params"]["name"]
            arguments = body["params"]["arguments"]
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "content": [
                            {"type": "text", "text": f"called {name} with {json.dumps(arguments)}"}
                        ],
                        "isError": False,
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "error": {"code": -32601, "message": f"method not found: {method}"},
            },
        )

    return handler


def _client(**kwargs: Any) -> GatewayClient:
    return GatewayClient(
        "https://gw.example.com",
        token="test-token",
        transport=httpx.MockTransport(_make_handler(**kwargs)),
    )


# ---------------------------------------------------------------------------
# Endpoint normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("api", "expected"),
    [
        ("https://gw.example.com", "https://gw.example.com/mcp"),
        ("https://gw.example.com/", "https://gw.example.com/mcp"),
        ("https://gw.example.com/mcp", "https://gw.example.com/mcp"),
        ("https://gw.example.com/mcp/", "https://gw.example.com/mcp"),
        ("https://gw.example.com/mcp?x=1", "https://gw.example.com/mcp"),
        ("http://localhost:3100", "http://localhost:3100/mcp"),
    ],
)
def test_normalize_endpoint(api: str, expected: str) -> None:
    assert _normalize_endpoint(api) == expected


@pytest.mark.parametrize("bad", ["", "not-a-url", "ftp://x", "/mcp"])
def test_normalize_endpoint_rejects_bad(bad: str) -> None:
    with pytest.raises(GatewayConfigError):
        _normalize_endpoint(bad)


def test_missing_api_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHALK_MCP_GATEWAY_API", raising=False)
    with pytest.raises(GatewayConfigError):
        GatewayClient()


def test_api_and_token_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHALK_MCP_GATEWAY_API", "https://env.example.com")
    monkeypatch.setenv("CHALK_MCP_GATEWAY_TOKEN", "env-token")
    client = GatewayClient(transport=httpx.MockTransport(_make_handler()))
    assert client.endpoint == "https://env.example.com/mcp"
    assert client._token == "env-token"


# ---------------------------------------------------------------------------
# Session handshake
# ---------------------------------------------------------------------------


def test_initialize_handshake_and_session_header() -> None:
    calls: list[dict[str, Any]] = []
    client = GatewayClient(
        "https://gw.example.com",
        token="test-token",
        transport=httpx.MockTransport(_make_handler(calls=calls)),
    )
    client.list_tool_specs()

    methods = [c["body"].get("method") for c in calls]
    assert methods[0] == "initialize"
    assert methods[1] == "notifications/initialized"
    assert "tools/list" in methods

    # initialize must NOT carry a session id; later requests must.
    assert "mcp-session-id" not in calls[0]["headers"]
    assert calls[-1]["headers"].get("mcp-session-id") == "sess-123"
    assert calls[-1]["headers"].get("authorization") == "Bearer test-token"

    # session is established once and reused
    client.list_tool_specs()
    assert [c["body"].get("method") for c in calls].count("initialize") == 1


# ---------------------------------------------------------------------------
# Tool resolution
# ---------------------------------------------------------------------------


def test_list_tools() -> None:
    tools = _client().list_tools()
    assert {t.name for t in tools} == {"gateway__whoami", "linear__create_issue"}


def test_from_name_exact() -> None:
    tool = Tool.from_name("linear__create_issue", client=_client())
    assert tool.name == "linear__create_issue"
    assert tool.description == "Create a Linear issue."
    assert tool.input_schema["required"] == ["title"]
    assert tool.namespace == "linear"
    assert tool.short_name == "create_issue"


@pytest.mark.parametrize(
    "name",
    ["linear.create_issue", "linear__create_issue", "LINEAR__CREATE_ISSUE"],
)
def test_from_name_canonicalization(name: str) -> None:
    tool = Tool.from_name(name, client=_client())
    assert tool.name == "linear__create_issue"


def test_from_name_via_sse() -> None:
    tool = Tool.from_name("linear.create_issue", client=_client(sse=True))
    assert tool.name == "linear__create_issue"


def test_from_name_not_found() -> None:
    with pytest.raises(ToolNotFoundError) as excinfo:
        Tool.from_name("nope__missing", client=_client())
    assert "linear__create_issue" in str(excinfo.value)
    assert excinfo.value.name == "nope__missing"


def test_tool_with_no_schema_defaults_empty() -> None:
    spec = Tool.from_spec({"name": "x__y"})
    assert spec.input_schema == {}


def test_tool_spec_requires_name() -> None:
    with pytest.raises(ValueError):
        Tool.from_spec({"description": "no name"})


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------


def test_call_with_kwargs() -> None:
    tool = Tool.from_name("linear__create_issue", client=_client())
    result = tool.call(title="flaky CI", teamId="ENG")
    assert not result.is_error
    assert "called linear__create_issue" in result.text
    assert '"title": "flaky CI"' in result.text


def test_call_with_mapping_and_kwargs_merge() -> None:
    tool = Tool.from_name("linear__create_issue", client=_client())
    result = tool.call({"title": "a"}, teamId="ENG")
    assert '"title": "a"' in result.text
    assert '"teamId": "ENG"' in result.text


def test_unbound_tool_cannot_call() -> None:
    with pytest.raises(ToolCallError):
        Tool(name="x__y").call(foo="bar")


def test_call_error_raises_by_default() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"mcp-session-id": "s"},
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-06-18"}},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": _TOOLS}},
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"content": [{"type": "text", "text": "boom"}], "isError": True},
            },
        )

    client = GatewayClient(
        "https://gw.example.com", token="t", transport=httpx.MockTransport(handler)
    )
    tool = Tool.from_name("linear__create_issue", client=client)
    with pytest.raises(ToolCallError):
        tool.call(title="x")
    # raise_on_error=False surfaces the error result instead
    result = tool.call(title="x", raise_on_error=False)
    assert result.is_error
    assert result.text == "boom"


def test_jsonrpc_error_raises_protocol_error() -> None:
    tool = Tool(
        name="unknown__method",
        _client=_client(),
    )
    with pytest.raises(GatewayProtocolError):
        # the fake gateway returns a JSON-RPC error for unknown methods
        tool._client.request("does/not/exist")  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


def test_openai_tool_from_tool() -> None:
    tool = Tool.from_name("linear__create_issue", client=_client())
    spec = OpenaiTool.from_tool(tool)
    assert spec == {
        "type": "function",
        "function": {
            "name": "linear__create_issue",
            "description": "Create a Linear issue.",
            "parameters": tool.input_schema,
        },
    }
    assert spec.name == "linear__create_issue"
    assert spec.source is tool
    # It is a plain dict — directly usable in OpenAI's tools=[...]
    assert isinstance(spec, dict)


def test_openai_tool_strict_and_responses_shape() -> None:
    tool = Tool.from_name("linear__create_issue", client=_client())
    spec = OpenaiTool.from_tool(tool, strict=True)
    assert spec["function"]["strict"] is True

    responses = spec.to_responses_tool()
    assert responses["type"] == "function"
    assert responses["name"] == "linear__create_issue"
    assert responses["strict"] is True
    assert "parameters" in responses
    assert "function" not in responses


def test_openai_tool_from_name_and_call() -> None:
    client = _client()
    spec = OpenaiTool.from_name("linear.create_issue", client=client)
    assert spec.name == "linear__create_issue"
    result = spec.call(title="x")
    assert "called linear__create_issue" in result.text


def test_openai_tool_empty_schema_default() -> None:
    spec = OpenaiTool.from_tool(Tool(name="gateway__whoami"))
    assert spec["function"]["parameters"] == {"type": "object", "properties": {}}


def test_anthropic_tool_from_tool() -> None:
    tool = Tool.from_name("linear__create_issue", client=_client())
    spec = AnthropicTool.from_tool(tool)
    assert spec == {
        "name": "linear__create_issue",
        "input_schema": tool.input_schema,
        "description": "Create a Linear issue.",
    }
    assert spec.name == "linear__create_issue"
    assert spec.source is tool
    assert isinstance(spec, dict)
