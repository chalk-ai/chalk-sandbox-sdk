"""Adapters that convert a resolved gateway :class:`Tool` into provider-native
tool specs.

Each adapter *is* the plain ``dict`` the provider SDK expects, so instances can
be dropped straight into a ``tools=[...]`` argument. They also keep a reference
to the source :class:`Tool` (``.source``) and expose :meth:`call` so a tool can
be both advertised to the model and executed from the same object.

``from`` is a Python keyword, so the adapter entry point is spelled
:meth:`from_tool` (with :meth:`from_name` for one-shot resolution)::

    from chalkcompute.mcp.gateway import OpenaiTool, Tool

    spec = OpenaiTool.from_tool(Tool.from_name("linear.create_issue"))
    # equivalently:
    spec = OpenaiTool.from_name("linear.create_issue")
"""

from __future__ import annotations

from typing import Any

from chalkcompute.mcp.gateway._client import GatewayClient
from chalkcompute.mcp.gateway._tool import Tool, ToolResult

# A permissive empty JSON Schema, used when a tool advertises no input schema.
_EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


class OpenaiTool(dict[str, Any]):
    """An OpenAI Chat Completions ``tools`` entry built from a gateway tool.

    The instance *is* the ``{"type": "function", "function": {...}}`` dict the
    OpenAI SDK accepts::

        tool = OpenaiTool.from_tool(Tool.from_name("linear__create_issue"))
        client.chat.completions.create(model="gpt-5", tools=[tool], messages=...)

    Use :meth:`to_responses_tool` for the flatter OpenAI *Responses* API shape.
    """

    __slots__ = ("_source",)
    _source: Tool

    @classmethod
    def from_tool(cls, tool: Tool, *, strict: bool | None = None) -> OpenaiTool:
        """Adapt a resolved :class:`Tool` into an OpenAI tool spec.

        Parameters
        ----------
        tool
            The resolved gateway tool.
        strict
            When set, emit OpenAI structured-outputs ``strict`` on the function.

        Returns
        -------
        OpenaiTool
            The OpenAI Chat Completions tool spec.
        """
        function: dict[str, Any] = {
            "name": tool.name,
            "parameters": dict(tool.input_schema) if tool.input_schema else dict(_EMPTY_SCHEMA),
        }
        if tool.description:
            function["description"] = tool.description
        if strict is not None:
            function["strict"] = strict
        spec = cls({"type": "function", "function": function})
        spec._source = tool
        return spec

    @classmethod
    def from_name(
        cls,
        name: str,
        *,
        client: GatewayClient | None = None,
        api: str | None = None,
        token: str | None = None,
        strict: bool | None = None,
    ) -> OpenaiTool:
        """Resolve a tool by name and adapt it in one step."""
        return cls.from_tool(
            Tool.from_name(name, client=client, api=api, token=token), strict=strict
        )

    @property
    def source(self) -> Tool:
        """The resolved :class:`Tool` this spec was built from."""
        return self._source

    @property
    def name(self) -> str:
        """The tool name advertised to the model."""
        return self["function"]["name"]

    def to_responses_tool(self) -> dict[str, Any]:
        """Return the flat OpenAI *Responses* API tool shape.

        Returns
        -------
        dict[str, Any]
            ``{"type": "function", "name", "parameters", ...}``.
        """
        function = self["function"]
        out: dict[str, Any] = {
            "type": "function",
            "name": function["name"],
            "parameters": function.get("parameters", dict(_EMPTY_SCHEMA)),
        }
        if "description" in function:
            out["description"] = function["description"]
        if "strict" in function:
            out["strict"] = function["strict"]
        return out

    def call(
        self, arguments: dict[str, Any] | None = None, /, **kwargs: Any
    ) -> ToolResult:
        """Invoke the underlying gateway tool (see :meth:`Tool.call`)."""
        return self._source.call(arguments, **kwargs)


class AnthropicTool(dict[str, Any]):
    """An Anthropic Messages API ``tools`` entry built from a gateway tool.

    The instance *is* the ``{"name", "description", "input_schema"}`` dict the
    Anthropic SDK accepts::

        tool = AnthropicTool.from_tool(Tool.from_name("linear__create_issue"))
        client.messages.create(model="claude-opus-4-8", tools=[tool], messages=...)
    """

    __slots__ = ("_source",)
    _source: Tool

    @classmethod
    def from_tool(cls, tool: Tool) -> AnthropicTool:
        """Adapt a resolved :class:`Tool` into an Anthropic tool spec."""
        spec = cls(
            {
                "name": tool.name,
                "input_schema": dict(tool.input_schema)
                if tool.input_schema
                else dict(_EMPTY_SCHEMA),
            }
        )
        if tool.description:
            spec["description"] = tool.description
        spec._source = tool
        return spec

    @classmethod
    def from_name(
        cls,
        name: str,
        *,
        client: GatewayClient | None = None,
        api: str | None = None,
        token: str | None = None,
    ) -> AnthropicTool:
        """Resolve a tool by name and adapt it in one step."""
        return cls.from_tool(Tool.from_name(name, client=client, api=api, token=token))

    @property
    def source(self) -> Tool:
        """The resolved :class:`Tool` this spec was built from."""
        return self._source

    @property
    def name(self) -> str:
        """The tool name advertised to the model."""
        return self["name"]

    def call(
        self, arguments: dict[str, Any] | None = None, /, **kwargs: Any
    ) -> ToolResult:
        """Invoke the underlying gateway tool (see :meth:`Tool.call`)."""
        return self._source.call(arguments, **kwargs)
