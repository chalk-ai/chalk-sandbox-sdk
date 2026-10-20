"""Tool resolution against the Chalk MCP gateway.

:class:`Tool` is a resolved, callable handle to a single gateway tool. Resolve
one by name with :meth:`Tool.from_name` and adapt it to a provider tool spec
(see :mod:`chalkcompute.mcp.gateway._adapters`), or invoke it directly with
:meth:`Tool.call`.
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass, field
from typing import Any

from chalkcompute.mcp.gateway._client import GatewayClient, get_default_client
from chalkcompute.mcp.gateway._errors import ToolCallError, ToolNotFoundError


def _canonical(name: str) -> str:
    """Canonicalize a tool name for matching.

    The gateway namespaces tools with ``__`` (``linear__create_issue``); this
    SDK also accepts the dotted form (``linear.create_issue``). Both collapse to
    the same canonical key so either spelling resolves the same tool. Matching
    is case-insensitive.

    Parameters
    ----------
    name
        A raw tool name (gateway- or dotted-form).

    Returns
    -------
    str
        The canonical match key.
    """
    return name.strip().replace("__", ".").lower()


def _resolve_client(
    client: GatewayClient | None,
    api: str | None,
    token: str | None,
) -> GatewayClient:
    """Pick the client to use for a resolution/call.

    A supplied ``client`` wins; otherwise a fresh client is built when ``api``
    or ``token`` is given, and the process-wide default is used otherwise.
    """
    if client is not None:
        return client
    if api is not None or token is not None:
        return GatewayClient(api, token=token)
    return get_default_client()


@dataclass(frozen=True)
class ToolResult:
    """The result of a ``tools/call`` against the gateway.

    Attributes
    ----------
    content
        The MCP content blocks (each a dict with a ``type`` such as ``"text"``).
    is_error
        Whether the tool reported a tool-level error (MCP ``isError``).
    structured_content
        The optional ``structuredContent`` payload, when the tool returns one.
    """

    content: list[dict[str, Any]] = field(default_factory=list)
    is_error: bool = False
    structured_content: dict[str, Any] | None = None

    @property
    def text(self) -> str:
        """The concatenated text of every ``text`` content block."""
        return "\n".join(
            block["text"]
            for block in self.content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )

    @classmethod
    def _from_result(cls, result: dict[str, Any]) -> ToolResult:
        content = result.get("content")
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        structured = result.get("structuredContent")
        return cls(
            content=blocks,
            is_error=bool(result.get("isError", False)),
            structured_content=structured if isinstance(structured, dict) else None,
        )

    def __str__(self) -> str:
        return self.text


@dataclass(frozen=True)
class Tool:
    """A resolved, callable handle to a single MCP gateway tool.

    Examples
    --------
    >>> from chalkcompute.mcp.gateway import Tool
    >>> tool = Tool.from_name("linear__create_issue")   # or "linear.create_issue"
    >>> tool.call(title="flaky CI", teamId="ENG")
    """

    name: str
    description: str | None = None
    input_schema: dict[str, Any] = field(default_factory=dict)
    _client: GatewayClient | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_spec(
        cls,
        spec: dict[str, Any],
        *,
        client: GatewayClient | None = None,
    ) -> Tool:
        """Build a :class:`Tool` from a raw MCP tool descriptor.

        Parameters
        ----------
        spec
            A raw MCP tool dict (``name`` / ``description`` / ``inputSchema``).
        client
            The gateway client to bind for :meth:`call`.

        Returns
        -------
        Tool
            The resolved tool.

        Raises
        ------
        ValueError
            If ``spec`` has no ``name``.
        """
        name = spec.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"Tool spec is missing a 'name': {spec!r}")
        description = spec.get("description")
        input_schema = spec.get("inputSchema")
        return cls(
            name=name,
            description=description if isinstance(description, str) else None,
            input_schema=dict(input_schema) if isinstance(input_schema, dict) else {},
            _client=client,
        )

    @classmethod
    def list(
        cls,
        *,
        client: GatewayClient | None = None,
        api: str | None = None,
        token: str | None = None,
    ) -> builtins.list[Tool]:  # `list` is shadowed by this classmethod in class scope
        """List every tool exposed by the gateway.

        Parameters
        ----------
        client
            An explicit :class:`GatewayClient`. Takes precedence over ``api`` /
            ``token``.
        api
            Gateway URI override (defaults to ``$CHALK_MCP_GATEWAY_API``).
        token
            Bearer token override.

        Returns
        -------
        list[Tool]
            The resolved tools.
        """
        resolved = _resolve_client(client, api, token)
        return [cls.from_spec(spec, client=resolved) for spec in resolved.list_tool_specs()]

    @classmethod
    def from_name(
        cls,
        name: str,
        *,
        client: GatewayClient | None = None,
        api: str | None = None,
        token: str | None = None,
    ) -> Tool:
        """Resolve a single tool by name against the gateway.

        Accepts either the gateway namespaced name (``linear__create_issue``)
        or the dotted form (``linear.create_issue``); matching is
        case-insensitive. An exact (byte-for-byte) match is preferred when the
        canonical form is ambiguous.

        Parameters
        ----------
        name
            The tool name to resolve.
        client
            An explicit :class:`GatewayClient`. Takes precedence over ``api`` /
            ``token``.
        api
            Gateway URI override (defaults to ``$CHALK_MCP_GATEWAY_API``).
        token
            Bearer token override.

        Returns
        -------
        Tool
            The resolved tool, bound to the resolving client.

        Raises
        ------
        ToolNotFoundError
            If no tool matches ``name``.
        """
        resolved = _resolve_client(client, api, token)
        specs = resolved.list_tool_specs()

        target = _canonical(name)
        matches = [
            spec
            for spec in specs
            if isinstance(spec.get("name"), str) and _canonical(spec["name"]) == target
        ]
        if not matches:
            raise ToolNotFoundError(
                name,
                available=[s["name"] for s in specs if isinstance(s.get("name"), str)],
            )
        exact = [spec for spec in matches if spec.get("name") == name]
        chosen = exact[0] if exact else matches[0]
        return cls.from_spec(chosen, client=resolved)

    @property
    def namespace(self) -> str | None:
        """The backend namespace (text before ``__``), or ``None`` if unprefixed."""
        prefix, sep, _ = self.name.partition("__")
        return prefix if sep else None

    @property
    def short_name(self) -> str:
        """The tool name without its backend namespace prefix."""
        _, sep, rest = self.name.partition("__")
        return rest if sep else self.name

    def call(
        self,
        arguments: dict[str, Any] | None = None,
        /,
        *,
        raise_on_error: bool = True,
        **kwargs: Any,
    ) -> ToolResult:
        """Invoke this tool through the gateway.

        Arguments may be passed as a single mapping, as keyword arguments, or
        both (keywords win on conflict).

        Parameters
        ----------
        arguments
            The tool arguments as a mapping (positional-only).
        raise_on_error
            If ``True`` (default), raise :class:`ToolCallError` when the tool
            reports ``isError``. If ``False``, return the error result.
        **kwargs
            Tool arguments as keyword arguments.

        Returns
        -------
        ToolResult
            The parsed call result.

        Raises
        ------
        ToolCallError
            If this tool is unbound, or the tool reports an error and
            ``raise_on_error`` is set.
        """
        if self._client is None:
            raise ToolCallError(
                f"Tool {self.name!r} is not bound to a gateway client; "
                "resolve it via Tool.from_name(...) before calling."
            )
        merged = dict(arguments or {})
        merged.update(kwargs)
        result = ToolResult._from_result(self._client.call_tool_raw(self.name, merged))
        if result.is_error and raise_on_error:
            raise ToolCallError(
                f"Tool {self.name!r} returned an error: {result.text}",
                result=result,
            )
        return result
