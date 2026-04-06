# MCP (Model Context Protocol)

Aar can act as an **MCP host** — connecting to one or more external MCP servers and exposing their tools as native agent tools. The core loop sees them identically to built-in tools; no provider-specific plumbing is needed.

```bash
pip install "aar-agent[mcp]"
```

The MCP bridge keeps the server connections **alive for the full lifetime of the session** — across every turn in an interactive chat, across every tool call in a multi-step task. Connections are cleanly closed when the bridge context exits.

## Quick start — CLI with a config file

The fastest way to attach MCP servers to any command is `--mcp-config`:

```bash
# Interactive chat with a filesystem server
aar chat --mcp-config mcp.json

# One-shot task
aar run "List the Python files in /tmp" --mcp-config mcp.json

# Resume a session and keep the same MCP tools available
aar resume <session-id> --mcp-config mcp.json

# See all registered tools (built-ins + MCP)
aar tools --mcp-config mcp.json

# Rich TUI with MCP tools
aar tui --mcp-config mcp.json
```

`--mcp-config` is supported by `chat`, `run`, `resume`, `tools`, and `tui`. The bridge is opened before the first prompt and closed after the last response.

## JSON config file format

Create a JSON file that lists the servers. Both a bare array and a `{"servers": [...]}` wrapper are accepted:

```json
{
  "servers": [
    {
      "name": "fs",
      "transport": "stdio",
      "command": "npx",
      "args": ["--prefer-offline", "@modelcontextprotocol/server-filesystem", "/tmp"]
    },
    {
      "name": "github",
      "transport": "stdio",
      "command": "uvx",
      "args": ["mcp-server-github"],
      "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_..."},
      "prefix_tools": true
    },
    {
      "name": "sentry",
      "transport": "http",
      "url": "https://mcp.sentry.io/mcp",
      "headers": {"Authorization": "Bearer sntrys_..."}
    }
  ]
}
```

All fields mirror `MCPServerConfig` exactly (see the reference table below).

## Connect programmatically — multiple servers

Use `MCPBridge` when you need full control over the lifecycle in application code:

```python
import asyncio
from agent import Agent, AgentConfig, ProviderConfig
from agent.extensions.mcp import MCPBridge, MCPServerConfig

servers = [
    MCPServerConfig(
        name="fs",
        transport="stdio",
        command="npx",
        args=["--prefer-offline", "@modelcontextprotocol/server-filesystem", "/tmp"],
    ),
    MCPServerConfig(
        name="github",
        transport="stdio",
        command="uvx",
        args=["mcp-server-github"],
        env={"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_..."},
        prefix_tools=True,        # -> "github__create_issue", "github__list_prs", ...
    ),
]

async def main():
    config = AgentConfig(
        provider=ProviderConfig(name="anthropic", model="claude-haiku-4-5-20251001")
    )

    async with MCPBridge(servers) as bridge:
        from agent.tools.registry import ToolRegistry
        registry = ToolRegistry()
        n = await bridge.register_all(registry)
        print(f"Registered {n} MCP tools")

        agent = Agent(config=config, registry=registry)
        agent.on_event(print)

        # bridge stays open for all turns
        session = await agent.run("List files in /tmp")
        session = await agent.run("Now show only .py files", session)

asyncio.run(main())
```

## Connect programmatically — single server

For simpler cases, use `MCPClient` directly:

```python
from agent.extensions.mcp import MCPClient, MCPServerConfig

cfg = MCPServerConfig(
    name="fs",
    transport="stdio",
    command="npx",
    args=["--prefer-offline", "@modelcontextprotocol/server-filesystem", "/tmp"],
)

async with MCPClient(cfg) as client:
    specs = await client.list_tools()          # -> list[ToolSpec]
    print([s.name for s in specs])

    output = await client.call_tool("list_directory", {"path": "/tmp"})
    print(output)                              # plain string for the LLM
```

## Load config from a file in code

```python
from agent.extensions.mcp import load_mcp_config, MCPBridge

servers = load_mcp_config("mcp.json")   # accepts {"servers":[...]} or bare [...]

async with MCPBridge(servers) as bridge:
    ...
```

## Connect to a remote HTTP server

```python
MCPServerConfig(
    name="myapi",
    transport="http",
    url="https://myserver.example.com/mcp",
    headers={"Authorization": "Bearer sk-..."},
)
```

## Avoid name collisions across servers

If two servers expose a tool with the same name, set `prefix_tools=True` on one (or both) to namespace them:

```python
MCPServerConfig(name="github", transport="stdio", command="uvx",
                args=["mcp-server-github"], prefix_tools=True)
# registers tools as "github__create_issue", "github__list_prs", etc.
```

Without `prefix_tools`, a name collision raises `ValueError` immediately — it is never silently ignored.

## MCPServerConfig reference

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | `str` | — | Logical name; used as tool prefix when `prefix_tools=True` |
| `transport` | `str` | `"stdio"` | `"stdio"` or `"http"` |
| `command` | `str` | `""` | Executable to launch (stdio only, e.g. `"npx"`, `"uvx"`, `"node"`) |
| `args` | `list[str]` | `[]` | Command-line arguments for the subprocess |
| `env` | `dict[str,str]` | `{}` | Extra environment variables merged into the subprocess env |
| `url` | `str` | `""` | Server URL (http only) |
| `headers` | `dict[str,str]` | `{}` | HTTP headers sent with every request (e.g. `Authorization`) |
| `prefix_tools` | `bool` | `False` | Prefix tool names with `"{name}__"` to avoid collisions |

## Supported MCP transports

| Transport | How it works | When to use |
|---|---|---|
| `stdio` | Spawns a local subprocess, communicates via stdin/stdout | Local tools (filesystem, git, databases) |
| `http` | HTTP POST + optional SSE (Streamable HTTP) | Remote or shared servers |

> **Windows / npx tip:** `npx -y` can stall on repeated calls when it tries to reach the npm
> registry without network access. Prefer `npx --prefer-offline` once the package is cached, or
> invoke `node <path-to-index.js>` directly to bypass the registry check entirely.

## MCP content types

MCP tool results can contain mixed content. Aar serializes all blocks to a plain string for the LLM:

| MCP block type | Serialized as |
|---|---|
| `TextContent` | The text value |
| `ImageContent` | `[image: mime/type]` |
| `EmbeddedResource` (text) | The text value |
| `EmbeddedResource` (blob) | `[blob resource: uri]` |

## MCP tools and the web server

The `serve` command does not yet support `--mcp-config`. To expose MCP tools over the web API, build the bridge and registry once and pass the registry to `create_asgi_app` — it is shared across all requests:

```python
import asyncio
import uvicorn
from agent.core.config import AgentConfig, ProviderConfig
from agent.extensions.mcp import MCPBridge, load_mcp_config
from agent.tools.registry import ToolRegistry
from agent.transports.web import create_asgi_app

async def main():
    servers = load_mcp_config("mcp.json")
    registry = ToolRegistry()

    async with MCPBridge(servers) as bridge:
        await bridge.register_all(registry)
        config = AgentConfig(
            provider=ProviderConfig(name="anthropic", model="claude-haiku-4-5-20251001")
        )
        # registry is shared — MCP tools are available to every request
        app = create_asgi_app(config, registry=registry)
        config_uvicorn = uvicorn.Config(app, host="0.0.0.0", port=8080)
        server = uvicorn.Server(config_uvicorn)
        await server.serve()   # bridge stays open for the server's lifetime

asyncio.run(main())
```
