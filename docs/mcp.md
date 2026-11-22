# MCP (Model Context Protocol)

Aar can act as an **MCP host** — connecting to one or more external MCP servers and exposing their tools as native agent tools. The core loop sees them identically to built-in tools; no provider-specific plumbing is needed.

```bash
pip install "aar-agent[mcp]"
```

### Supported SDK versions

Aar requires **`mcp>=1.28.1,<2.0`**.

- The **lower** bound skips 1.27.0/1.27.1, which carry three published
  advisories (PYSEC-2026-3481/3482/3483). All three affect MCP *servers*, and
  Aar is a client, but the floor should not permit them.
- The **upper** bound is a hard incompatibility, not caution. The 2.x SDK
  renamed the wire model to snake_case (`Tool.inputSchema` → `input_schema`,
  `CallToolResult.isError` → `is_error`), renamed
  `streamablehttp_client` → `streamable_http_client`, dropped that function's
  `headers=` argument in favour of a caller-supplied client, and moved from
  `httpx` to `httpx2`. Tool discovery fails on 2.x for *every* transport, so
  the cap is required until the bridge grows a compatibility layer. The SDK
  itself recommends pinning `mcp<2` to keep v1 client code running.

Note that most of Aar's MCP tests mock the SDK, so they pass on any version —
the constraint above comes from round-trip testing against a real server
(`tests/test_windows_mcp.py` also runs one real stdio round trip).

A server's `instructions` (from its `initialize` result) are added to the
system prompt as a tool guideline (`MCP server 'name': …`, at most 2000
characters), so small models learn what the server is for — without them a
model may answer "I cannot operate programs" while holding the tools.

The MCP bridge keeps the server connections **alive for the full lifetime of the session** — across every turn in an interactive chat, across every tool call in a multi-step task. Connections are cleanly closed when the bridge context exits.

## Quick start — CLI with a config file

The fastest way to attach MCP servers to any command is `--mcp-config`:

```bash
# Interactive chat with a filesystem server
aar chat --mcp-config mcp.json

# One-shot task
aar run "List the Python files in /tmp" --mcp-config mcp.json

# Resume a session and keep the same MCP tools available
aar chat --session <session-id> --mcp-config mcp.json

# See all registered tools (built-ins + MCP)
aar tools --mcp-config mcp.json

# Rich TUI with MCP tools
aar tui --mcp-config mcp.json
```

`--mcp-config` is supported by `chat`, `run`, `tools`, and `tui`. `serve` accepts the flag but currently emits a warning and ignores it — see [MCP tools and the web server](#mcp-tools-and-the-web-server) for the programmatic workaround. The bridge is opened before the first prompt and closed after the last response.

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

## Tool annotations and approvals

MCP servers can describe each tool with
[annotations](https://modelcontextprotocol.io/specification/2025-06-18/server/tools#tool-annotations).
Aar uses `readOnlyHint` / `destructiveHint` to decide whether a tool call needs
the user's approval:

| Server declares | Aar side effects | `review` mode (default) | `auto` mode | `read-only` mode |
|---|---|---|---|---|
| nothing (no annotations) | `external` | runs | runs | runs |
| `readOnlyHint: true` | `external` | runs | runs | runs |
| `readOnlyHint: false`, or `destructiveHint: true` without `readOnlyHint: true` | `external`, `execute` | **asks first** | runs ¹ | blocked |

¹ Like `bash`: while `allowed_paths` is active and the sandbox mode does not
enforce it at the OS level (the default `local`), Aar cannot verify what the
call touches and still asks.

The question goes through the normal approval path — the terminal prompt in
`chat`/`tui`, the editor's permission dialog over ACP — and a denied call never
reaches the server. Unannotated tools keep the old behaviour, so check what a
third-party server declares before relying on it.

A result the server marks `isError` is reported as a failed tool call
(`Error [exception]: MCPToolError: <server message>`), not as normal output.

## MCP content types

MCP tool results can contain mixed content. Aar turns the blocks into the tool
result's text, and keeps images as images:

| MCP block type | Text | Also sent |
|---|---|---|
| `TextContent` | The text value | |
| `ImageContent` | `[image: mime/type, size]` | the image, to vision models |
| `EmbeddedResource` (text) | The text value | |
| `EmbeddedResource` (`image/*` blob) | `[image: mime/type, size]` | the image, to vision models |
| `EmbeddedResource` (other blob) | `[blob resource: uri]` | |

### Images

When the active model supports vision (`supports_vision`, see
[providers](providers.md)), it gets the image together with the text; otherwise
it gets the text placeholder plus `[1 image(s) not shown: the active model does
not accept images]`. How the image travels depends on the provider:

| Provider | Where the image goes |
|---|---|
| Anthropic | `image` blocks inside the `tool_result` |
| Gemini | `inlineData` parts after the `functionResponse` parts of the same turn |
| OpenAI, generic | a `user` message right after the turn's `tool` messages (Chat Completions allows images only in user messages) |
| Ollama | a `user` message with an `images` list right after the `tool` messages |

Limits, in the `tools` config section:

- `max_image_bytes` (default 3 750 000, decoded) — a bigger image is dropped
  and the text says `[image omitted: 5.1 MB exceeds tools.max_image_bytes
  (3.6 MB)]`. Aar does not re-encode images, so ask the tool for a smaller one
  (the Windows `screenshot` tool takes `max_width`). Only PNG, JPEG, GIF and
  WebP are passed on; others become a note too.
- `keep_recent_images` (default 3) — only the images of the most recent 3
  tool results that returned any keep their data, in the context *and* in the
  saved session JSONL. Older ones shrink to
  their placeholder plus `[1 image(s) dropped from history; call the tool
  again to see it]`, so long desktop sessions don't grow without bound.
  `0` never sends a tool image to the model.

Over ACP the image is also shown on the tool-call card (an ACP `image` content
block).

Your own tools can return images the same way: return
`agent.tools.schema.ToolOutput(text=..., images=[ImageURLBlock(...)])` with
`data:` URIs (`agent.core.multimodal.image_block_from_base64` builds one).

## Bundled server: Windows desktop automation

`tools/windows_mcp/` lets the agent operate ordinary Windows programs — Notepad,
Excel, Word, Explorer, settings dialogs — the way a screen reader sees them: it
reads the window's accessibility tree (Windows UI Automation), then presses
buttons, fills in fields and reads text by element, not by guessing screen
coordinates. Keystrokes and screenshots are fallbacks for programs that expose
little to UI Automation.

### Requirements

- Windows 10 or 11, and an **interactive desktop session**: the server must run
  as the signed-in user whose windows it operates (not as a service, over SSH or
  in a locked session).
- Python 3.12+ with `pip install -e ".[windows]"` from the repo root (pulls `mcp`
  and `uiautomation`/`comtypes`; on other systems the extra installs nothing
  Windows-specific). `tools/windows_mcp/requirements.txt` has the same two
  packages for a server-only environment.
- Programs running as administrator cannot be controlled from a server that is
  not elevated (Windows blocks that on purpose).

### Enable it in the CLI

Run from the repo root (the config adds `tools/` to `sys.path`):

```bash
aar chat --mcp-config tools/mcp_windows.json
aar tui  --mcp-config tools/mcp_windows.json
aar tools --mcp-config tools/mcp_windows.json   # shows windows__list_windows, ...
```

To have it in every session, put the server entry into `~/.aar/mcp_servers.json`
with an absolute path instead of `'tools'`. The tools are registered with the
prefix `windows__` (`prefix_tools: true`).

### Enable it over ACP (Zed)

Add a context server; Zed forwards it to Aar when it opens a session (see
[ACP — MCP servers from the editor](acp.md#mcp-servers-from-the-editor)). Use
the `python.exe` of the environment where `aar-agent[windows]` is installed and
an absolute path to `tools`:

```json
{
  "context_servers": {
    "aar-windows": {
      "command": "C:/path/to/aar/.venv/Scripts/python.exe",
      "args": [
        "-c",
        "import sys; sys.path.insert(0, 'C:/path/to/aar/tools'); from windows_mcp.server import mcp; mcp.run(transport='stdio')"
      ],
      "env": {}
    }
  }
}
```

### Tools

| Tool | What it does | Approval in `review` mode |
|---|---|---|
| `list_windows` | Visible windows: handle, program, pid, title | no |
| `focus_window` | Bring a window to the front (restore if minimized) | no |
| `get_ui_tree` | The window's controls as a compact list with ids (`e12`), types, names, values and states | no |
| `read_text` | Text/value of one control, or a window's text; for a browser or document window the page text in reading order (`offset` to read on) | no |
| `screenshot` | PNG of a window or the screen | no |
| `launch_app` | Start a program or open a document; returns the new window(s) | **yes** |
| `click_element` | Press a button, toggle a checkbox, select an item, open a menu (or a real click) | **yes** |
| `set_text` | Replace the text of a field or document; `press_enter` submits it (address bar, search box) | **yes** |
| `send_keys` | Press keys and shortcuts (`{Ctrl}s`, `{Alt}{F4}`, `{Enter}`) | **yes** |
| `click_at` | Real click at a position inside an element or the window (canvas, map, slider track) | **yes** |
| `drag` | Hold the mouse button and move through a path: draw on a canvas, move a slider, drag and drop | **yes** |
| `close_window` | Close a window like clicking X (a save prompt may appear) | **yes** |

A window is addressed by its handle from `list_windows` or by part of its title
(`"Notepad"`); an element by its id from the **latest** `get_ui_tree` of that
window or by a selector — `name=Save`, `automation_id=SaveButton`,
`type=Button;name=OK`. Example tree:

```text
Window "Untitled - Notepad"
e1 MenuBar #MenuBar
  e2 MenuItem "File" [collapsed]
  e3 MenuItem "Edit" [collapsed]
e4 Document "Text editor" value="Hello"
e5 Button "Close"
```

`click_at` and `drag` take positions as **fractions** of the element (or of the
window's client area when no element is given): `[0, 0]` is the top-left corner,
`[1, 1]` the bottom-right, `[0.5, 0.5]` the centre. This works for any window
size or DPI and needs no screenshot. Example — a sunset in Paint, using only its
toolbar (all buttons and colours are in the tree) and the canvas element
(`automation_id=image`): `click_element "Füllen"` + colour + `click_at` fills
the canvas; `"Linie"` + `drag [[0, 0.6], [1, 0.6]]` draws the horizon;
`"Oval"` + `drag [[0.44, 0.45], [0.56, 0.75]]` the sun; the brush with a
multi-point `drag` draws freehand waves. The server sends real mouse input
(`SendInput`, with intermediate moves so freehand strokes keep their shape) and
runs per-monitor DPI aware, so element rectangles and the mouse agree on scaled
monitors.

Web pages: `set_text` on Edge's or Chrome's address bar with `press_enter`
opens a page or a search; `read_text` without an element then returns the
page's text (UI Automation TextPattern of the document) instead of the
browser's toolbar and a list of control names. Long pages are cut at
`max_chars` (6000) with a note which `offset` to pass for the next part.

`launch_app` accepts the name from the Start menu (`"Excel"`, `"Calculator"`),
an exe name (`"notepad"`, `"winword"`), a full path or a document; it searches
`PATH`, the registry's App Paths and the Start menu (which covers Store apps).

### Safety

- Every tool carries MCP annotations: the five that only look are
  `readOnlyHint: true`; the seven that change something are `readOnlyHint: false`
  (`destructiveHint: true`, except `launch_app`, which only adds a window). In
  `review` mode Aar therefore asks before each change and shows the call (for
  example `windows__set_text` with the window, element and text); in `read-only`
  mode changes are blocked; see [the table above](#tool-annotations-and-approvals).
- The read-only tools are **not** prompted, and they can see whatever is on the
  screen (documents, mail, chat windows). Values of password fields are never
  returned.
- `click_at` and `drag` refuse positions covered by another window, like clicks.
- `send_keys` can do anything a keyboard can (including `{Win}r` and typing a
  command), so treat its approval like a shell command.
- Keys and clicks are only sent once the target window really is in the
  foreground, and a click is refused when another window covers the element —
  nothing is typed into the wrong window. If Windows refuses to switch, the tool
  says so and asks the user to click the window.
- Errors are short and tell the model what to do next ("Unknown element id …
  Call get_ui_tree again.", "Could not find a program called …").
- `screenshot` returns a PNG (1280 px wide by default, `max_width` to shrink
  it). A vision model sees the image ([Images](#images)); any other model only
  the placeholder and size, so `get_ui_tree` and `read_text` stay the main way
  to read a window. A screenshot shows everything on the screen, and it is sent
  to the model provider.

### Tests

```bash
# any OS: core logic, the server on a fake backend, a real stdio round trip
# through the Aar bridge and approval path
pytest tests/test_windows_mcp.py tests/test_mcp.py

# Windows only: drives Notepad and System Information (msinfo32) on the real desktop.
# Needs --live and AAR_DESKTOP_TESTS=1 (or AAR_SANDBOX=1); best run in Windows Sandbox.
AAR_DESKTOP_TESTS=1 pytest tests/live/test_windows_mcp_live.py --live
```

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
