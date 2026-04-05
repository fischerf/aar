# Aar — Adaptive Action & Reasoning Agent

[![Website](https://img.shields.io/badge/website-fischerf.github.io%2Faar-blue?style=flat-square)](https://fischerf.github.io/aar/)

A lean, provider-agnostic agent framework with a thin core loop, typed event model, sandboxed tool execution, and pluggable transports. **Aar** stands for *Adaptive Action & Reasoning*.

## Design goals

- **Thin core loop** — the main execution path is small and readable at a glance
- **Typed event model** — every message, tool call, and result is a typed, serializable event
- **Provider-agnostic** — swap between Anthropic, OpenAI, Ollama, or any OpenAI-compatible endpoint without changing agent code
- **Safe by default** — path restrictions, command deny-lists, and approval gates built in
- **Modular transports** — the same agent runs from CLI, TUI, web API, or embedded in your code
- **Persistent sessions** — every run is saved as JSONL and resumable; long sessions can be compacted
- **Observable** — every provider call and tool execution is timed; sessions carry a `trace_id`
- **Cancellable** — cooperative (`asyncio.Event`) and hard (`CancelledError`) cancellation built in

## Installation

```bash
# Everything at once
pip install "aar-agent[all,dev]"

# or provider specific
pip install "aar-agent[anthropic]"
pip install "aar-agent[openai]"
pip install "aar-agent[ollama]"   # Ollama uses httpx, already a core dep
pip install "aar-agent[generic]"  # Generic uses httpx, already a core dep

# or with Ollama provider + MCP support
pip install "aar-agent[ollama,mcp]"

# Core only (no LLM provider)
pip install aar-agent
```

> **Note:** `aar-agent` is not published to PyPI.  
> Use the **from-source install** instructions below.

### Installing from source (development)

Clone the repo and install in *editable* mode so that changes to the source
files take effect immediately without reinstalling:

```bash
git clone https://github.com/fischerf/aar.git
cd aar

# Full dev setup — includes pytest, pytest-asyncio, and ruff
pip install -e ".[all,dev]"

# Core only
pip install -e .

# With a provider and MCP support
pip install -e ".[anthropic,mcp]"
```

The `-e` flag creates a live link from `site-packages` back to the source
tree. Editing any file under `agent/` is reflected instantly in the
installed package — no `pip install` step needed between changes.

To verify the install:

```bash
aar --help          # CLI entry-point should be on your PATH
pytest tests/ -v      # run the full test suite (214 tests, no API keys required)
```

## Quick start

Set `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or point `base_url` at a local Ollama instance.

## CLI

```bash
# Interactive chat (workspace sandbox on by default — asks before write/execute,
# file tools restricted to cwd)
aar chat

# Chat with a specific provider/model
aar chat --provider openai --model gpt-4o
aar chat --provider ollama --model llama3

# Disable the workspace sandbox for full access
aar chat --no-require-approval --no-restrict-to-cwd

# Run a one-shot task (same approval defaults as chat — prompts before write/execute)
aar run "Refactor main.py to use async/await"

# Skip approval prompts for scripted / CI use
aar run --no-require-approval "Refactor main.py to use async/await"

# Run with path restriction too (belt-and-suspenders for automated mode)
aar run --require-approval --restrict-to-cwd "Delete unused imports"

# Load full config from a JSON file
aar chat --config aar.json

# Resume a previous session (works with chat, run, and tui)
aar chat --session <session-id>
aar run "follow up task" --session <session-id>
aar tui --session <session-id>

# List saved sessions
aar sessions

# List available tools
aar tools

# Launch the rich TUI (workspace sandbox on, like chat)
aar tui

# Start the HTTP/SSE web server
aar serve --host 0.0.0.0 --port 8080
```

### Verbose mode

Pass `--verbose` (or `-v`) to `chat`, `run`, or `tui` to enable richer
operation feedback:

- **Side-effect badge** before each tool name — `[read]`, `[write]`, `[exec]`, `[net]`, `[ext]`
- **Path highlighting** — file paths in tool arguments are shown in blue
- **Timing** — execution duration is appended to each result panel title (e.g. `Result: edit_file 42ms`)

```bash
aar chat --verbose
aar run "refactor src/main.py" --verbose
aar tui --verbose
aar tui --verbose --mcp-config tools/mcp_servers.json --provider ollama --model qwen3.5:9b
```

or programmatic:

```python
import asyncio
from agent import Agent, AgentConfig, ProviderConfig

config = AgentConfig(
    provider=ProviderConfig(name="anthropic", model="claude-sonnet-4-20250514"),
    system_prompt="You are a helpful coding assistant.",
)

agent = Agent(config=config)

async def main():
    session = await agent.run("List all Python files in the current directory")
    print(session.state)  # AgentState.COMPLETED

asyncio.run(main())
```

## Configuration

```python
from agent import AgentConfig, ProviderConfig, SafetyConfig, ToolConfig

config = AgentConfig(
    provider=ProviderConfig(
        name="anthropic",                          # "anthropic" | "openai" | "ollama" | "generic"
        model="claude-sonnet-4-20250514",
        api_key="...",                             # or set via env var
        max_tokens=4096,
        temperature=0.0,
    ),
    tools=ToolConfig(
        enabled_builtins=["read_file", "write_file", "edit_file", "list_directory", "bash"],
        command_timeout=30,                        # seconds
        max_output_chars=50_000,
    ),
    safety=SafetyConfig(
        read_only=False,                           # block all writes
        require_approval_for_writes=False,         # ask before every write
        require_approval_for_execute=False,        # ask before every shell command
        denied_paths=["**/.env", "**/*.key"],      # glob patterns (see docs/safety.md for defaults)
        allowed_paths=[],                          # whitelist (empty = allow all non-denied)
        sandbox="local",                           # "local" | "subprocess"
    ),
    max_steps=50,
    timeout=300.0,                                 # seconds
    system_prompt="You are a helpful assistant.",
    session_dir=".agent/sessions",
    shell_path="",                                 # custom shell binary (see below)
    project_rules_dir=".agent",                    # project rules folder (see below)
    log_level="WARNING",                           # DEBUG | INFO | WARNING | ERROR | CRITICAL
)
```

### Config loading and precedence

All CLI modes and the web transport load configuration from multiple sources. The order of precedence (highest wins):

| Source | `aar chat` / `aar run` / `aar tui` | `aar serve` | `WebTransport()` programmatic | `Agent()` programmatic |
|--------|:----------------------------------:|:-----------:|:-----------------------------:|:----------------------:|
| Explicit CLI flag | ✓ highest | ✓ (fewer flags — see [Web API](#web-api)) | — | — |
| `--config <file>` | ✓ | ✓ | — | — |
| `~/.aar/config.json` | ✓ auto-discovered | ✓ auto-discovered | ✓ auto-discovered | ❌ not loaded |
| Built-in defaults | ✓ lowest | ✓ | ✓ | ✓ only source unless you pass `config=` |

When using `Agent()` directly in code, the config file is **not** loaded automatically — pass a config explicitly if you need it:

```python
from pathlib import Path
from agent.core.config import load_config
from agent import Agent

config = load_config(Path("~/.aar/config.json").expanduser())
agent = Agent(config=config)
```

### Approval behaviour by mode

`SafetyConfig.require_approval_for_writes` and `require_approval_for_execute` default to `True`. What happens when a tool triggers an approval check depends on the transport:

| Mode | Approval behaviour |
|------|--------------------|
| `aar chat`, `aar tui`, `aar run` | **Terminal prompt** — `y` / `n` / `always` |
| `aar serve` / `WebTransport` | **Auto-approved** — the HTTP request is treated as implicit approval |
| `Agent()` programmatic (no callback) | **Denied** — logged as *"No approval callback configured"* |

To disable approval prompts in terminal mode entirely:

```bash
aar chat --no-require-approval
aar run --no-require-approval "do something"
```

Or set it permanently in `~/.aar/config.json`:

```json
{
  "safety": {
    "require_approval_for_writes": false,
    "require_approval_for_execute": false
  }
}
```

To inject a custom approval callback for the web transport (e.g. call a webhook before each write):

```python
from agent.safety.permissions import ApprovalCallback, ApprovalResult
from agent.transports.web import create_asgi_app

async def my_approval(spec, tc) -> ApprovalResult:
    # call your external system here
    return ApprovalResult.APPROVED

app = create_asgi_app(config, approval_callback=my_approval)
```

To supply an approval callback when using `Agent()` directly:

```python
from agent import Agent, AgentConfig
from agent.safety.permissions import ApprovalResult

async def my_approval(spec, tc) -> ApprovalResult:
    print(f"Allow {tc.tool_name}? (y/n) ", end="")
    return ApprovalResult.APPROVED if input().strip().lower() == "y" else ApprovalResult.DENIED

agent = Agent(config=AgentConfig(), approval_callback=my_approval)
```

### Log level

Control how much the agent logs to stderr. The default is `WARNING`, which only shows errors and warnings (including friendly provider error messages). Use `DEBUG` to see full request traces, raw tracebacks, and internal loop steps.

| Level | What you see |
|-------|-------------|
| `DEBUG` | Everything — full tracebacks, HTTP traces, step-by-step loop internals |
| `INFO` | Step counts, provider timing, tool execution summaries |
| `WARNING` | Provider errors, safety policy hits, unexpected conditions **(default)** |
| `ERROR` | Only hard failures |
| `CRITICAL` | Silent except for fatal errors |

**Via config file** (`~/.aar/config.json` or `--config`):

```json
{
  "log_level": "DEBUG"
}
```

**Via `AgentConfig` in code:**

```python
config = AgentConfig(log_level="DEBUG")
```

**Via CLI flag** (overrides the config file for that run):

```bash
aar chat --log-level DEBUG
aar run "do something" --log-level INFO
aar tui --log-level WARNING
```

### Configurable system prompt

By default, the system prompt is assembled automatically from up to three layers:

| Layer | Source | Purpose |
|-------|--------|---------|
| **Base** | built-in | Runtime facts — OS, working directory, shell |
| **Global rules** | `~/.aar/rules.md` | Personal preferences that apply to all projects |
| **Project rules** | `<project_rules_dir>/rules.md` | Project-specific instructions (checked into git) |

Each layer is optional. If no rules files exist, the agent behaves exactly as before — only the base prompt is used. When present, the layers are concatenated in order, separated by `---`.

**Global rules** — create `~/.aar/rules.md` for preferences that follow you across projects:

```markdown
# My rules
- Always use type hints on public functions.
- Prefer pathlib over os.path.
- Use ruff for formatting.
```

**Project rules** — create `<project_rules_dir>/rules.md` (default `.agent/rules.md`) for instructions specific to the current repo:

```markdown
# Project rules
- This is a FastAPI app. Use pytest-asyncio for async tests.
- Follow the existing service pattern in app/services/.
```

The project rules folder defaults to `.agent` and can be changed via `project_rules_dir` (see below).

**Override** — if you pass `system_prompt` explicitly to `AgentConfig`, the auto-assembly is skipped entirely and your string is used as-is.

### Configurable shell

By default, Aar uses Git Bash (`bash -c`) on Windows and the system shell (`/bin/sh`) on Unix for tool execution. Override this with `shell_path` to use a specific shell binary — for example WSL bash, zsh, or fish:

**Via config file** (`~/.aar/config.json` or `--config`):

```json
{
  "shell_path": "/usr/bin/zsh"
}
```

**Via `AgentConfig` in code:**

```python
config = AgentConfig(shell_path="/usr/bin/zsh")
```

On Windows, common values include:

| Shell | Typical path |
|-------|-------------|
| Git Bash | `bash` (default — found via PATH) |
| WSL bash | `wsl.exe` (note: uses `-c` flag) |
| PowerShell | Not supported (requires `-Command`, not `-c`) |

When `shell_path` is set, it is used everywhere: the built-in `bash` tool, sandbox execution, and the system prompt sent to the model.

### Configurable project rules directory

The project rules directory defaults to `.agent`. Change it with `project_rules_dir` so the agent reads `<project_rules_dir>/rules.md` instead of `.agent/rules.md`:

**Via config file:**

```json
{
  "project_rules_dir": ".config/aar"
}
```

**Via `AgentConfig` in code:**

```python
config = AgentConfig(project_rules_dir=".config/aar")
```

This only affects where project rules are loaded from. The `session_dir` is configured independently.

## Image input (multimodal)

Aar supports image input for vision-capable models on all four providers. Pass a list of `ContentBlock` objects instead of a plain string to `Agent.run()`, `Agent.chat()`, or `Session.add_user_message()`.

```python
from agent.core.events import TextBlock, ImageURLBlock, ImageURL

# HTTP / HTTPS URL
response = await agent.chat([
    TextBlock(text="What is shown in this diagram?"),
    ImageURLBlock(image_url=ImageURL(url="https://example.com/diagram.png")),
])

# Local file — base-64 encode it first
import base64
raw = open("screenshot.png", "rb").read()
data_uri = "data:image/png;base64," + base64.b64encode(raw).decode()

response = await agent.chat([
    TextBlock(text="Describe this screenshot."),
    ImageURLBlock(image_url=ImageURL(url=data_uri)),
])

# OpenAI vision detail hint ("auto" | "low" | "high")
ImageURLBlock(image_url=ImageURL(url="https://example.com/photo.jpg", detail="high"))
```

Text-only callers are completely unchanged — passing a plain string still works.

### Provider support

| Provider | Vision | Notes |
|---|---|---|
| Anthropic | ✓ always | claude-3+ models; HTTP URLs and base-64 data URIs |
| OpenAI | ✓ auto-detected | gpt-4o, gpt-4-vision, o1 and newer; all image types |
| Ollama | ✓ default on | Model must be vision-capable (e.g. `qwen2.5vl`, `llava`, `minicpm-v`) |
| Generic | ✓ auto-detected | Any OpenAI-compatible endpoint with vision support |

Vision support is auto-detected from the model name for OpenAI and Generic providers. For Ollama, it defaults to `True` and can be overridden:

```python
ProviderConfig(
    name="ollama",
    model="qwen2.5vl:7b",
    extra={"supports_vision": True},   # default True; set False to opt out
)
```

Check capability at runtime:

```python
print(agent.provider.capabilities().vision)  # True / False
```

### Format conversion

The same `ContentBlock` API works identically across all providers. Aar converts internally:

- **OpenAI / Generic** — content blocks forwarded as-is (already the OpenAI wire format)
- **Anthropic** — `image_url` blocks converted to `{"type": "image", "source": {...}}`; `data:` URIs become `base64` sources, HTTP URLs become `url` sources
- **Ollama** — sent as an OpenAI-compatible content array (Ollama 0.5+); `data:` URI payloads are also placed in the legacy `images` field for Ollama < 0.5

### Ollama vision models

Pull any vision-capable model and point the provider at it:

```bash
ollama pull qwen2.5vl:7b
aar chat --provider ollama --model qwen2.5vl:7b
```

Popular choices: `qwen2.5vl:7b`, `llava:13b`, `minicpm-v`, `moondream`. Once a `qwen3.5:9b` Ollama model is published it will work with the same config — Qwen3.5 is a native vision-language model.

### Multi-turn with images

Images in earlier turns are preserved in `session.to_messages()` — subsequent text-only turns can refer back to them:

```python
session = None

session = await agent.run(
    [TextBlock(text="Here is our UI mockup."),
     ImageURLBlock(image_url=ImageURL(url="https://example.com/mockup.png"))],
    session=session,
)
session = await agent.run("Now write the HTML for it.", session=session)
```

### Accessing content blocks directly

```python
from agent.core.events import TextBlock, ImageURLBlock, ImageURL, ContentBlock

# Build a typed block list
parts: list[ContentBlock] = [
    TextBlock(text="Analyse this chart."),
    ImageURLBlock(image_url=ImageURL(url="https://example.com/chart.png", detail="high")),
]

# Session helper
from agent.core.session import Session
s = Session()
msg = s.add_user_message(parts)
print(msg.is_multimodal)   # True
print(msg.content)         # "Analyse this chart."  (text summary for logging)
```

## Providers

### Anthropic

```python
from agent import AgentConfig, ProviderConfig

config = AgentConfig(provider=ProviderConfig(
    name="anthropic",
    model="claude-sonnet-4-20250514",
    api_key="sk-ant-...",         # or ANTHROPIC_API_KEY env var
))
```

Supports: tools, streaming, extended thinking (reasoning blocks).

### OpenAI

```python
config = AgentConfig(provider=ProviderConfig(
    name="openai",
    model="gpt-4o",
    api_key="sk-...",             # or OPENAI_API_KEY env var
))
```

Compatible with any OpenAI-compatible API (Azure, Together, etc.) via `base_url`.

### Ollama

```python
config = AgentConfig(provider=ProviderConfig(
    name="ollama",
    model="llama3.2",
    base_url="http://localhost:11434",   # default
    extra={"keep_alive": "10m"},
))
```

Enable reasoning extraction for models like `deepseek-r1`:

```python
ProviderConfig(name="ollama", model="deepseek-r1", extra={"supports_reasoning": True})
```

Enable vision for models with a vision encoder (see [Image input](#image-input-multimodal)):

```python
ProviderConfig(name="ollama", model="qwen2.5vl:7b", extra={"supports_vision": True})
```

### Generic (OpenAI-compatible)

Any OpenAI-compatible HTTP endpoint, using a custom `api-key` header for authentication.

```python
config = AgentConfig(provider=ProviderConfig(
    name="generic",
    model="gpt-4o-2024-08-06",
    api_key="...",           # or GENERIC_API_KEY env var
    extra={
        "endpoint": "https://api.provider.com/gpt/gpt-5.1",
        # Optional overrides:
        # "extra_headers": {"X-Trace-Id": "abc123"},
        # "timeout": 120.0,
        # "response_format": "json_object",  # "text" | "json_object" | "json_schema"
    },
))
```

The endpoint URL can also be set via the `GENERIC_ENDPOINT` environment variable.
Supports: tools, streaming, structured output (`json_object` / `json_schema`).

Install: `pip install aar-agent[generic]` (uses `httpx`, already included in the base install).

## Tool system

### Built-in tools

| Tool | Side effect | Description |
|---|---|---|
| `read_file` | read | Read a file with line numbers |
| `write_file` | write | Write a file, creating directories as needed |
| `edit_file` | write | Replace an exact string in a file (must be unique) |
| `list_directory` | read | List files and directories |
| `bash` | execute | Run a shell command, return stdout + stderr |

All built-ins are opt-in via `ToolConfig.enabled_builtins`.

### Custom tools

```python
from agent import Agent
from agent.tools.schema import SideEffect, ToolSpec

agent = Agent()

# Decorator style
@agent.registry.register(
    name="fetch_url",
    description="Fetch the contents of a URL",
    side_effects=[SideEffect.NETWORK],
)
async def fetch_url(url: str) -> str:
    import httpx
    async with httpx.AsyncClient() as client:
        r = await client.get(url)
        return r.text

# Or explicit ToolSpec
agent.registry.add(ToolSpec(
    name="count_lines",
    description="Count the lines in a file",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
    side_effects=[SideEffect.READ],
    handler=lambda path: str(sum(1 for _ in open(path))),
))
```

## Safety

Aar has a layered safety system with sensible defaults. See [`docs/safety.md`](docs/safety.md) for the full reference, including the complete list of denied paths/commands, per-transport defaults, CLI flags, sandbox modes, and the approval callback API.

**Key features at a glance:**

- **Workspace sandbox** — `aar chat`, `aar tui`, and `aar run` all require approval before writes/shell commands by default (terminal prompt). `aar chat` and `aar tui` also restrict file tools to the current working directory by default. The web transport (`aar serve` / `WebTransport`) auto-approves — no terminal is available. Toggle with `--[no-]require-approval` and `--[no-]restrict-to-cwd`.
- **Built-in deny lists** — credential files, key material, `.env` files, and dangerous shell commands (25+ path patterns, 20+ command patterns) are always blocked.
- **Human approval** — supply a custom `ApprovalCallback` that returns `APPROVED`, `DENIED`, or `APPROVED_ALWAYS`.
- **Configurable policy** — set via CLI flags, a JSON config file (see [Configuration](#configuration)), or a `SafetyConfig` object.

```python
from agent import SafetyConfig

SafetyConfig(read_only=True)                        # block all writes and shell commands
SafetyConfig(require_approval_for_writes=True)       # prompt before writes
SafetyConfig(allowed_paths=["/my/project/**"])        # restrict file access
```

## Sessions and persistence

Sessions are automatically saved as JSONL files. Every event (messages, tool calls, results, metadata) is persisted. Each session carries a `session_id`, a `run_id` (refreshed on resume), and a `trace_id` (stable for the lifetime of the session object).

```python
from agent import Agent
from agent.memory.session_store import SessionStore

agent = Agent()
store = SessionStore(".agent/sessions")

# First run
session = await agent.run("Write a Python script that sorts a CSV")
store.save(session)
print(session.session_id)  # e.g. "a3f1b2c4d5e6f7a8"
print(session.trace_id)    # stable identifier for logging / tracing

# Resume later
session = store.load("a3f1b2c4d5e6f7a8")
session = await agent.run("Now add error handling", session=session)

# List all sessions
print(store.list_sessions())

# Compact a long session to its most recent 200 events
store.compact("a3f1b2c4d5e6f7a8", max_events=200)
```

From the CLI, resume a session by passing `--session` (or `-s`) to any interactive
command:

```bash
aar chat --session a3f1b2c4d5e6f7a8
aar run "add error handling" --session a3f1b2c4d5e6f7a8
aar tui --session a3f1b2c4d5e6f7a8
```

## Event model

The agent emits typed events you can subscribe to:

```python
from agent.core.events import AssistantMessage, ToolCall, ToolResult, EventType

def on_event(event):
    if isinstance(event, ToolCall):
        print(f"→ {event.tool_name}({event.arguments})")
    elif isinstance(event, ToolResult) and event.is_error:
        print(f"✗ {event.tool_name}: {event.output}")
    elif isinstance(event, AssistantMessage):
        print(event.content)

agent.on_event(on_event)
session = await agent.run("Do something")
```

Event types: `user_message`, `assistant_message`, `tool_call`, `tool_result`, `reasoning`, `provider_meta`, `error`, `session`.

Timing fields are populated automatically by the runtime:
- `ProviderMeta.duration_ms` — wall time for the provider API call
- `ToolResult.duration_ms` — wall time for tool execution

## Cancellation

Pass an `asyncio.Event` to stop the loop cooperatively between steps:

```python
import asyncio
from agent.core.loop import run_loop

cancel = asyncio.Event()

# Cancel from another coroutine or thread
asyncio.get_event_loop().call_later(5.0, cancel.set)

session = await run_loop(session, provider, executor, config, cancel_event=cancel)
# session.state == AgentState.CANCELLED
```

Hard cancellation via `asyncio` task cancellation also works — the loop catches `CancelledError`, sets state to `CANCELLED`, and re-raises.

## Observability

Aggregate timing and token usage from any session:

```python
from agent.extensions.observability import session_metrics

m = session_metrics(session)
print(f"steps={m.total_steps}")
print(f"tokens={m.total_tokens}  (in={m.total_input_tokens} out={m.total_output_tokens})")
print(f"provider_ms={m.total_provider_duration_ms:.0f}")
print(f"tool_ms={m.total_tool_duration_ms:.0f}  calls={m.total_tool_calls}")
print(f"errors={m.total_errors}")

# Per-step breakdown
for step in m.steps:
    print(f"  step {step.step}: provider={step.provider_duration_ms:.0f}ms  tools={step.total_tool_duration_ms:.0f}ms")
```

`session_metrics()` reads all events once; it does not require a live provider or executor.

## MCP (Model Context Protocol)

Aar can act as an **MCP host** — connecting to one or more external MCP servers and exposing their tools as native agent tools. The core loop sees them identically to built-in tools; no provider-specific plumbing is needed.

```bash
pip install "aar-agent[mcp]"
```

The MCP bridge keeps the server connections **alive for the full lifetime of the session** — across every turn in an interactive chat, across every tool call in a multi-step task. Connections are cleanly closed when the bridge context exits.

### Quick start — CLI with a config file

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

### JSON config file format

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

### Connect programmatically — multiple servers

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
        prefix_tools=True,        # → "github__create_issue", "github__list_prs", …
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

### Connect programmatically — single server

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
    specs = await client.list_tools()          # → list[ToolSpec]
    print([s.name for s in specs])

    output = await client.call_tool("list_directory", {"path": "/tmp"})
    print(output)                              # plain string for the LLM
```

### Load config from a file in code

```python
from agent.extensions.mcp import load_mcp_config, MCPBridge

servers = load_mcp_config("mcp.json")   # accepts {"servers":[…]} or bare […]

async with MCPBridge(servers) as bridge:
    ...
```

### Connect to a remote HTTP server

```python
MCPServerConfig(
    name="myapi",
    transport="http",
    url="https://myserver.example.com/mcp",
    headers={"Authorization": "Bearer sk-..."},
)
```

### Avoid name collisions across servers

If two servers expose a tool with the same name, set `prefix_tools=True` on one (or both) to namespace them:

```python
MCPServerConfig(name="github", transport="stdio", command="uvx",
                args=["mcp-server-github"], prefix_tools=True)
# registers tools as "github__create_issue", "github__list_prs", etc.
```

Without `prefix_tools`, a name collision raises `ValueError` immediately — it is never silently ignored.

### MCPServerConfig reference

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

### Supported MCP transports

| Transport | How it works | When to use |
|---|---|---|
| `stdio` | Spawns a local subprocess, communicates via stdin/stdout | Local tools (filesystem, git, databases) |
| `http` | HTTP POST + optional SSE (Streamable HTTP) | Remote or shared servers |

> **Windows / npx tip:** `npx -y` can stall on repeated calls when it tries to reach the npm
> registry without network access. Prefer `npx --prefer-offline` once the package is cached, or
> invoke `node <path-to-index.js>` directly to bypass the registry check entirely.

### MCP content types

MCP tool results can contain mixed content. Aar serializes all blocks to a plain string for the LLM:

| MCP block type | Serialized as |
|---|---|
| `TextContent` | The text value |
| `ImageContent` | `[image: mime/type]` |
| `EmbeddedResource` (text) | The text value |
| `EmbeddedResource` (blob) | `[blob resource: uri]` |

### MCP tools and the web server

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

## Web API

```bash
pip install uvicorn
aar serve --port 8080
```

### Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Health check |
| `/chat` | POST | Run a prompt, return full response |
| `/chat/stream` | POST | Run a prompt, stream events via SSE |
| `/sessions` | GET | List session IDs |
| `/sessions/{id}` | GET | Session details |

### `aar serve` flags

`aar serve` shares the same config-loading logic as `aar chat`/`aar run` but exposes a smaller set of flags:

| Flag | `aar chat` / `aar run` / `aar tui` | `aar serve` |
|------|:----------------------------------:|:-----------:|
| `--model`, `--provider`, `--api-key`, `--base-url` | ✓ | ✓ |
| `--config <file>` | ✓ | ✓ |
| `--read-only / --no-read-only` | ✓ | ✓ |
| `--host`, `--port` | — | ✓ |
| `--require-approval / --no-require-approval` | ✓ | — |
| `--restrict-to-cwd / --no-restrict-to-cwd` | ✓ | — |
| `--denied-paths`, `--allowed-paths` | ✓ | — |
| `--log-level` | ✓ | — |
| `--max-steps` | ✓ | — |
| `--session` | ✓ | — |
| `--mcp-config` | ✓ | — (see [MCP tools and the web server](#mcp-tools-and-the-web-server)) |

Config not expressible via `aar serve` flags can be set in `~/.aar/config.json` — the server auto-loads it on startup.

### Approval in the web transport

There is no terminal to prompt in a server process, so the web transport **auto-approves** all tool calls by default. The HTTP request itself is treated as implicit approval. This means `require_approval_for_writes` / `require_approval_for_execute` in `SafetyConfig` have no blocking effect — use `read_only` or path restrictions instead if you need hard limits.

```bash
# Harden the server: block all writes
aar serve --read-only

# Or restrict to a specific directory tree via config file
# ~/.aar/config.json
# { "safety": { "allowed_paths": ["/my/project/**"] } }
```

### Per-request safety override

Clients can tighten or loosen safety settings for a single request by including a `"safety"` key in the JSON body. Only the fields you specify are overridden; everything else uses the server's config.

```bash
# Force read-only for this one request
curl -X POST http://localhost:8080/chat \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Summarise README.md", "safety": {"read_only": true}}'

# Allow writes but restrict to a specific path
curl -X POST http://localhost:8080/chat \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Write hello.py", "safety": {"allowed_paths": ["/tmp/**"]}}'
```

### `/chat` — request and response

```bash
curl -X POST http://localhost:8080/chat \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Write hello.py", "session_id": null}'
```

Response JSON shape:

```json
{
  "session_id": "a3f1b2c4d5e6",
  "state":      "completed",
  "step_count": 2,
  "response":   "Here is the file I wrote.",
  "tool_results": [
    {
      "tool_name":   "write_file",
      "output":      "Written 42 bytes to hello.py",
      "is_error":    false,
      "duration_ms": 3.1
    }
  ],
  "events": [ ... ]
}
```

| Field | Description |
|-------|-------------|
| `response` | Final assistant text. If the model completed via tools without producing any narrating text, this falls back to the last successful tool output so you always get something meaningful. |
| `tool_results` | Ordered list of every tool call result in the run. Empty when no tools were used. |
| `state` | `"completed"` \| `"error"` \| `"cancelled"` — use this to detect failures cleanly. |
| `events` | Full ordered event log: `user_message`, `tool_call`, `tool_result`, `assistant_message`, `provider_meta`, `session` (ended), etc. Inspect these when you need the fine-grained trace. |

### `/chat/stream` — SSE event stream

```bash
curl -N http://localhost:8080/chat/stream \
  -X POST -H "Content-Type: application/json" \
  -d '{"prompt": "Write hello.py"}'
```

Events arrive as standard SSE frames, one per agent event:

```
event: tool_call
data: {"type":"tool_call","tool_name":"write_file","arguments":{"path":"hello.py","content":"..."},...}

event: tool_result
data: {"type":"tool_result","tool_name":"write_file","output":"Written 42 bytes","is_error":false,...}

event: assistant_message
data: {"type":"assistant_message","content":"Done — hello.py has been created.","stop_reason":"end_turn",...}

event: session
data: {"type":"session","data":{"state":"completed","step_count":2},"action":"ended",...}
```

**The `session` event with `action: "ended"` is the definitive done signal.** It is always emitted as the last event before the stream closes, and carries `data.state` (`"completed"` / `"error"` / `"cancelled"`) and `data.step_count`. Do not rely solely on stream-close to detect completion — the ended event lets you distinguish a clean finish from a network drop.

Summary of all event types you may receive:

| SSE `event:` field | When emitted | Key fields |
|--------------------|--------------|------------|
| `provider_meta` | After each LLM call | `usage`, `duration_ms`, `model` |
| `reasoning` | Extended-thinking models only | `content` |
| `tool_call` | Before each tool executes | `tool_name`, `arguments` |
| `tool_result` | After each tool finishes | `tool_name`, `output`, `is_error`, `duration_ms` |
| `assistant_message` | Each LLM text turn | `content`, `stop_reason` (`end_turn` \| `tool_use`) |
| `error` | Provider or safety failure | `message`, `recoverable` |
| `session` | Stream start and **stream end** | `action` (`"started"` \| `"ended"`), `data.state` |

### Embed the ASGI app

```python
from agent.transports.web import create_asgi_app
from agent.core.config import load_config
from pathlib import Path
import uvicorn

# Explicit config (or omit to auto-load ~/.aar/config.json)
config = load_config(Path("myconfig.json"))

app = create_asgi_app(config)
uvicorn.run(app, host="0.0.0.0", port=8080)
```

`create_asgi_app` accepts three optional arguments:

| Argument | Default | Description |
|----------|---------|-------------|
| `config` | `None` | `AgentConfig`. If `None`, auto-loads `~/.aar/config.json` or uses built-in defaults. |
| `approval_callback` | `_auto_approve_callback` | Async callable `(ToolSpec, ToolCall) -> ApprovalResult`. Override for webhook-style approval. |
| `registry` | `None` | Shared `ToolRegistry`. Used to expose MCP tools across all requests (see above). |

## Architecture

The project follows a modular design: a thin core loop, provider adapters, a tool registry with a safety pipeline, session persistence, and pluggable transports. See [`docs/architecture.md`](docs/architecture.md) for a detailed walkthrough of every component, the core loop, the event emission order, provider internals, the tool execution pipeline, and the safety architecture.

```
agent/
├── core/           # Loop, agent, events, session, config
├── providers/      # LLM API adapters (Anthropic, OpenAI, Ollama, Generic)
├── tools/          # Tool registry, schema, execution engine
├── safety/         # Policy engine, permission manager, sandboxes
├── memory/         # Session persistence (JSONL)
├── extensions/     # MCP bridge, observability
└── transports/     # CLI, TUI, web, event stream
```

## Testing

```bash
pip install "aar-agent[dev]"
pytest tests/ -v
```

The test suite (236 tests) runs entirely without live API calls using a `MockProvider`. Tests cover:

- Loop termination, max steps, timeout, cancellation (`asyncio.Event` + `CancelledError`), provider errors
- Session persistence, resumption, compaction, `trace_id` round-trip, message conversion
- Event serialization round-trips for all event types, including `duration_ms` fields
- Provider normalization for Anthropic, OpenAI, and Ollama (mocked)
- Tool registry, schema inference, execution (sync/async, timeout, truncation, timing)
- Safety policy (command deny-list, path restrictions, read-only mode, approval gates)
- Sandbox execution and timeout
- `session_metrics()` aggregation (timing, tokens, errors, per-step breakdown)
- MCP bridge: tool discovery, handler dispatch, content serialization, name collision detection, stdio/http transports (all mocked — no real MCP server required)

### Live testing against real providers

Live tests hit actual provider APIs and are skipped by default. Pass `--live` to enable them.

#### Ollama (local, no API key required)

```bash
# Pull a model first
ollama pull qwen3.5:9b

# Run the live CLI tests
pytest tests/test_cli.py -m live --live -v
```

The live test class (`TestLiveOllama`) uses `qwen3.5:9b` by default. To use a different model, edit the `MODEL` constant in `tests/test_cli.py` or run a one-off check via the CLI:

```bash
aar run "Reply with the word PONG." --provider ollama --model llama3.2
```

#### Anthropic

```bash
export ANTHROPIC_API_KEY=sk-ant-...
pytest tests/test_providers.py -m live --live -k Anthropic -v
```

Uses `claude-haiku-4-5-20251001` by default (cheapest model). Covers plain text, tool calls, stop-reason normalization, and provider meta. Quick smoke-test via CLI:

```bash
aar run "Reply with the word PONG." --provider anthropic --model claude-haiku-4-5-20251001
```

#### OpenAI (or any OpenAI-compatible endpoint)

```bash
export OPENAI_API_KEY=sk-...
pytest tests/test_providers.py -m live --live -k OpenAI -v
```

Uses `gpt-4o-mini` by default. Compatible endpoints (Azure, Together, etc.) can be tested by setting `base_url` in `ProviderConfig`.

#### Running all live tests together

```bash
# All providers (Anthropic + OpenAI + Ollama CLI tests)
pytest tests/ -m live --live -v

# Single provider
pytest tests/test_providers.py -m live --live -k Anthropic -v
pytest tests/test_providers.py -m live --live -k OpenAI -v
pytest tests/test_cli.py -m live --live -v           # Ollama
```

Tests for providers whose API key is not set will fail with an authentication error rather than being skipped — only export keys for the providers you want to exercise.

## Requirements

- Python 3.11+
- `pydantic >= 2.0`
- `httpx >= 0.27`
- `typer >= 0.12`
- `rich >= 13.0`
- Provider SDK as needed: `anthropic`, `openai`

### Windows — `bash` tool

The `bash` built-in tool requires a Unix-compatible shell on Windows. **Either** of the following is sufficient:

| Option | Install | Notes |
|--------|---------|-------|
| **Git for Windows** (Git Bash) | [git-scm.com](https://git-scm.com/download/win) | Lightweight; adds `bash` to `PATH`; drives mounted as `/c/`, `/d/`, … |
| **WSL** (Windows Subsystem for Linux) | `wsl --install` in an admin terminal | Full Linux environment; drives mounted as `/mnt/c/`, `/mnt/d/`, … |

> **If both are installed**, WSL's `bash.exe` (`C:\Windows\System32\bash.exe`) is found first by Windows `CreateProcess` before `PATH` is consulted, so WSL's bash will run. Keep this in mind when referencing file paths inside bash commands — use the appropriate mount prefix for whichever shell is active.

Neither is required if you do not enable the `bash` built-in tool (`ToolConfig(enabled_builtins=[...])`).

---

## License

Apache License 2.0
