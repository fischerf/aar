"""MCP (Model Context Protocol) bridge — exposes external MCP server tools
as native agent tools.

Each MCP server gets its own MCPClient (one dedicated connection per server,
as the MCP spec requires). MCPBridge manages all clients and registers their
tools into a ToolRegistry. The core loop sees them as ordinary ToolSpec entries
and calls them the same way it calls built-in tools.

Transports supported:
  - ``stdio`` — local subprocess via stdin/stdout (zero network overhead)
  - ``http``  — remote server via Streamable HTTP (HTTP POST + optional SSE)

Installation::

    pip install "epa-agent[mcp]"   # pulls in the official `mcp` SDK

Usage::

    import asyncio
    from agent.extensions.mcp import MCPBridge, MCPServerConfig
    from agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    servers = [
        MCPServerConfig(
            name="fs",
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        ),
        MCPServerConfig(
            name="myapi",
            transport="http",
            url="https://myserver.example.com/mcp",
            headers={"Authorization": "Bearer sk-..."},
        ),
    ]

    async def main():
        async with MCPBridge(servers) as bridge:
            n = await bridge.register_all(registry)
            print(f"Registered {n} MCP tools")
            # registry now contains all remote tools — pass it to ToolExecutor as normal

    asyncio.run(main())
"""

from __future__ import annotations

import json
import logging
from contextlib import AsyncExitStack
from typing import Any

from pydantic import BaseModel, Field

from agent.tools.registry import ToolRegistry
from agent.tools.schema import SideEffect, ToolSpec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class MCPServerConfig(BaseModel):
    """Configuration for a single MCP server connection."""

    name: str
    """Logical name for this server. Used as a tool prefix when prefix_tools=True."""

    transport: str = "stdio"
    """Transport type: ``"stdio"`` or ``"http"``."""

    # STDIO fields
    command: str = ""
    """Executable to launch (e.g. ``"python"``, ``"npx"``, ``"uvx"``). Required for stdio."""
    args: list[str] = Field(default_factory=list)
    """Command-line arguments passed to the subprocess."""
    env: dict[str, str] = Field(default_factory=dict)
    """Extra environment variables merged into the subprocess environment."""

    # HTTP fields
    url: str = ""
    """Server URL for HTTP transport (e.g. ``"https://myserver.example.com/mcp"``)."""
    headers: dict[str, str] = Field(default_factory=dict)
    """HTTP headers included in every request (e.g. ``Authorization``)."""

    prefix_tools: bool = False
    """Prefix every tool name with ``"{name}__"`` to avoid collisions between servers."""


# ---------------------------------------------------------------------------
# Single-server client
# ---------------------------------------------------------------------------


class MCPClient:
    """Manages a single dedicated connection to one MCP server.

    Use as an async context manager::

        async with MCPClient(config) as client:
            specs = await client.list_tools()
            output = await client.call_tool("my_tool", {"arg": "value"})
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._exit_stack = AsyncExitStack()
        self._session = None

    async def __aenter__(self) -> MCPClient:
        _require_mcp()

        if self.config.transport == "stdio":
            read, write = await self._connect_stdio()
        elif self.config.transport == "http":
            read, write = await self._connect_http()
        else:
            raise ValueError(
                f"Unknown MCP transport: {self.config.transport!r}. Use 'stdio' or 'http'."
            )

        from mcp import ClientSession

        self._session = await self._exit_stack.enter_async_context(
            ClientSession(read, write)
        )
        await self._session.initialize()
        logger.info(
            "Connected to MCP server %r (%s)", self.config.name, self.config.transport
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._exit_stack.aclose()
        self._session = None
        logger.info("Disconnected from MCP server %r", self.config.name)

    async def _connect_stdio(self):
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        if not self.config.command:
            raise ValueError(
                f"MCP server {self.config.name!r}: 'command' is required for stdio transport"
            )
        params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args,
            env=self.config.env or None,
        )
        read, write = await self._exit_stack.enter_async_context(stdio_client(params))
        return read, write

    async def _connect_http(self):
        if not self.config.url:
            raise ValueError(
                f"MCP server {self.config.name!r}: 'url' is required for http transport"
            )
        try:
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError:
            # Older SDK versions used a different module path
            from mcp.client.http import streamablehttp_client  # type: ignore[no-redef]

        read, write, _ = await self._exit_stack.enter_async_context(
            streamablehttp_client(self.config.url, headers=self.config.headers or None)
        )
        return read, write

    async def list_tools(self) -> list[ToolSpec]:
        """Discover available tools from this server and return them as ToolSpecs."""
        self._assert_connected()
        response = await self._session.list_tools()
        specs = []
        for tool in response.tools:
            local_name = (
                f"{self.config.name}__{tool.name}"
                if self.config.prefix_tools
                else tool.name
            )
            spec = ToolSpec(
                name=local_name,
                description=tool.description or "",
                input_schema=tool.inputSchema or {"type": "object", "properties": {}},
                side_effects=[SideEffect.EXTERNAL],
                handler=_make_tool_handler(self, tool.name),
            )
            specs.append(spec)
            logger.debug(
                "Discovered MCP tool %r from server %r", local_name, self.config.name
            )
        return specs

    async def call_tool(self, mcp_tool_name: str, arguments: dict[str, Any]) -> str:
        """Call a named tool on the MCP server and return its output as a string."""
        self._assert_connected()
        result = await self._session.call_tool(mcp_tool_name, arguments)
        return _content_to_str(result.content)

    def _assert_connected(self) -> None:
        if self._session is None:
            raise RuntimeError(
                "MCPClient is not connected. Use it as an async context manager."
            )


# ---------------------------------------------------------------------------
# Multi-server bridge
# ---------------------------------------------------------------------------


class MCPBridge:
    """Manages connections to multiple MCP servers and registers all their tools.

    Usage::

        async with MCPBridge(configs) as bridge:
            count = await bridge.register_all(registry)
    """

    def __init__(self, configs: list[MCPServerConfig]) -> None:
        self._configs = configs
        self._clients: list[MCPClient] = []
        self._exit_stack = AsyncExitStack()

    async def __aenter__(self) -> MCPBridge:
        for cfg in self._configs:
            client = await self._exit_stack.enter_async_context(MCPClient(cfg))
            self._clients.append(client)
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._exit_stack.aclose()
        self._clients.clear()

    async def register_all(self, registry: ToolRegistry) -> int:
        """Discover tools from all servers and register them into *registry*.

        Returns the total number of tools registered. Raises ``ValueError`` on
        name collisions — set ``prefix_tools=True`` on the conflicting server to
        namespace its tools.
        """
        count = 0
        for client in self._clients:
            specs = await client.list_tools()
            for spec in specs:
                if spec.name in registry:
                    raise ValueError(
                        f"MCP tool name collision: {spec.name!r} already exists in the "
                        f"registry. Set prefix_tools=True on server "
                        f"{client.config.name!r} to namespace its tools."
                    )
                registry.add(spec)
                count += 1
                logger.info(
                    "Registered MCP tool %r from server %r",
                    spec.name,
                    client.config.name,
                )
        return count

    @property
    def clients(self) -> list[MCPClient]:
        """The active MCPClient instances (one per configured server)."""
        return list(self._clients)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_mcp() -> None:
    try:
        import mcp  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "The 'mcp' package is required for MCP support. "
            "Install it with: pip install 'epa-agent[mcp]'"
        ) from exc


def _make_tool_handler(client: MCPClient, mcp_tool_name: str):
    """Return an async handler that delegates to the MCP server."""

    async def handler(**kwargs: Any) -> str:
        return await client.call_tool(mcp_tool_name, kwargs)

    handler.__name__ = mcp_tool_name
    return handler


def _content_to_str(content: list[Any]) -> str:
    """Serialize MCP response content blocks to a plain string.

    Handles TextContent, ImageContent, and EmbeddedResource blocks.
    Unknown block types fall back to ``str()``.
    """
    parts = []
    for block in content:
        if hasattr(block, "text") and not hasattr(block, "resource"):
            # TextContent
            parts.append(block.text)
        elif hasattr(block, "data") and hasattr(block, "mimeType"):
            # ImageContent
            parts.append(f"[image: {block.mimeType}]")
        elif hasattr(block, "resource"):
            # EmbeddedResource
            resource = block.resource
            if hasattr(resource, "text"):
                parts.append(resource.text)
            elif hasattr(resource, "blob"):
                parts.append(
                    f"[blob resource: {getattr(resource, 'uri', 'unknown')}]"
                )
            else:
                dumped = (
                    resource.model_dump()
                    if hasattr(resource, "model_dump")
                    else str(resource)
                )
                parts.append(json.dumps(dumped))
        else:
            parts.append(str(block))
    return "\n".join(parts)
