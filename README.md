# Aar — Adaptive Action & Reasoning Agent

A lean, provider-agnostic agent framework with a thin core loop, typed event model, sandboxed tool execution, and pluggable transports. **Aar** stands for *Adaptive Action & Reasoning*.

## Design goals

- **Thin core loop** — the main execution path is small and readable at a glance
- **Typed event model** — every message, tool call, and result is a typed, serializable event
- **Provider-agnostic** — swap between Anthropic, OpenAI, and Ollama without changing agent code
- **Safe by default** — path restrictions, command deny-lists, and approval gates built in
- **Modular transports** — the same agent runs from CLI, TUI, web API, or embedded in your code
- **Persistent sessions** — every run is saved as JSONL and resumable; long sessions can be compacted
- **Observable** — every provider call and tool execution is timed; sessions carry a `trace_id`
- **Cancellable** — cooperative (`asyncio.Event`) and hard (`CancelledError`) cancellation built in

## Installation

```bash
# Core only (no LLM provider)
pip install aar-agent

# With a specific provider
pip install "aar-agent[anthropic]"
pip install "aar-agent[openai]"
pip install "aar-agent[ollama]"   # Ollama uses httpx, already a core dep

# With MCP support
pip install "aar-agent[mcp]"

# Development
pip install -e .

#pip install "aar-agent[anthropic,mcp,dev]"
```

## Quick start

Set `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or point `base_url` at a local Ollama instance.

## CLI

```bash
# Interactive chat
agent chat

# Chat with a specific provider/model
agent chat --provider openai --model gpt-4o
agent chat --provider ollama --model llama3

# Run a one-shot task
agent run "Refactor main.py to use async/await"

# Resume a previous session
agent resume <session-id>

# List saved sessions
agent sessions

# List available tools
agent tools

# Launch the rich TUI
agent tui

# Start the HTTP/SSE web server
agent serve --host 0.0.0.0 --port 8080
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
        name="anthropic",                          # "anthropic" | "openai" | "ollama"
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
        denied_paths=["**/.env", "**/*.key"],      # glob patterns
        allowed_paths=[],                          # whitelist (empty = allow all non-denied)
        denied_commands=["rm -rf /", "mkfs"],      # substring matches
        sandbox="local",                           # "local" | "subprocess"
    ),
    max_steps=50,
    timeout=300.0,                                 # seconds
    system_prompt="You are a helpful assistant.",
    session_dir=".agent/sessions",
)
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

### Policy modes

```python
from agent import SafetyConfig

# Read-only: blocks all writes and shell commands
SafetyConfig(read_only=True)

# Require human approval before any write
SafetyConfig(require_approval_for_writes=True)

# Require approval before any shell command
SafetyConfig(require_approval_for_execute=True)

# Restrict file access to a specific directory
SafetyConfig(allowed_paths=["/my/project/**"])
```

### Human approval callback

```python
from agent.safety.permissions import ApprovalResult, PermissionManager
from agent.tools.execution import ToolExecutor

async def my_approval_callback(spec, tool_call) -> ApprovalResult:
    print(f"Allow {spec.name}({tool_call.arguments})? [y/n/always]")
    answer = input().strip().lower()
    if answer == "always":
        return ApprovalResult.APPROVED_ALWAYS
    return ApprovalResult.APPROVED if answer == "y" else ApprovalResult.DENIED

executor = ToolExecutor(
    registry,
    tool_config,
    SafetyConfig(require_approval_for_execute=True),
    approval_callback=my_approval_callback,
)
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

Aar can act as an **MCP host** — connecting to one or more external MCP servers and exposing their tools as native agent tools. The core loop sees them identically to built-in tools.

```bash
pip install "aar-agent[mcp]"
```

### Connect to a local stdio server

```python
import asyncio
from agent import Agent, AgentConfig, ProviderConfig
from agent.extensions.mcp import MCPBridge, MCPServerConfig
from agent.tools.registry import ToolRegistry
from agent.tools.execution import ToolExecutor
from agent.core.config import ToolConfig, SafetyConfig

servers = [
    MCPServerConfig(
        name="fs",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
    ),
]

async def main():
    registry = ToolRegistry()

    async with MCPBridge(servers) as bridge:
        n = await bridge.register_all(registry)
        print(f"Registered {n} MCP tools: {registry.names()}")

        executor = ToolExecutor(registry, ToolConfig(), SafetyConfig())
        config = AgentConfig(provider=ProviderConfig(name="anthropic", model="claude-haiku-4-5-20251001"))
        agent = Agent(config=config, tool_executor=executor)
        session = await agent.run("List files in /tmp")
        print(session.state)

asyncio.run(main())
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

If two servers expose tools with the same name, use `prefix_tools=True` to namespace them:

```python
MCPServerConfig(name="github", transport="stdio", command="uvx", args=["mcp-server-github"], prefix_tools=True)
# registers tools as "github__create_issue", "github__list_prs", etc.
```

Without `prefix_tools`, a collision raises `ValueError` immediately so it is never silently ignored.

### Supported MCP transports

| Transport | How it works | When to use |
|---|---|---|
| `stdio` | Spawns a local subprocess, communicates via stdin/stdout | Local tools (filesystem, git, databases) |
| `http` | HTTP POST + optional SSE (Streamable HTTP) | Remote or shared servers |

### MCP content types

MCP tool results can contain mixed content. Aar serializes all blocks to a plain string for the LLM:

| MCP block type | Serialized as |
|---|---|
| `TextContent` | The text value |
| `ImageContent` | `[image: mime/type]` |
| `EmbeddedResource` (text) | The text value |
| `EmbeddedResource` (blob) | `[blob resource: uri]` |

## Web API

```bash
pip install uvicorn
agent serve --port 8080
```

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Health check |
| `/chat` | POST | Run a prompt, return full response |
| `/chat/stream` | POST | Run a prompt, stream events via SSE |
| `/sessions` | GET | List session IDs |
| `/sessions/{id}` | GET | Session details |

```bash
curl -X POST http://localhost:8080/chat \
  -H "Content-Type: application/json" \
  -d '{"prompt": "List files in /tmp", "session_id": null}'

# Stream events
curl -N http://localhost:8080/chat/stream \
  -X POST -H "Content-Type: application/json" \
  -d '{"prompt": "Write hello.py"}'
```

Or embed the ASGI app directly:

```python
from agent.transports.web import create_asgi_app
import uvicorn

app = create_asgi_app(config)
uvicorn.run(app, host="0.0.0.0", port=8080)
```

## Architecture

```
agent/
├── core/
│   ├── loop.py          # Thin agent loop (~80 lines)
│   ├── agent.py         # High-level Agent class
│   ├── events.py        # Typed event model
│   ├── session.py       # Session (history + message conversion)
│   ├── state.py         # AgentState enum
│   └── config.py        # AgentConfig, ProviderConfig, SafetyConfig
├── providers/
│   ├── base.py          # Provider ABC + ProviderCapabilities
│   ├── anthropic.py     # Anthropic Messages API adapter
│   ├── openai.py        # OpenAI Chat Completions adapter
│   └── ollama.py        # Ollama REST API adapter
├── tools/
│   ├── registry.py      # Tool registry (decorator + explicit)
│   ├── schema.py        # ToolSpec, SideEffect
│   ├── execution.py     # ToolExecutor (policy + sandbox + run)
│   └── builtin/         # read_file, write_file, edit_file, list_dir, bash
├── safety/
│   ├── policy.py        # SafetyPolicy (ALLOW / DENY / ASK)
│   ├── permissions.py   # PermissionManager (approval gates)
│   └── sandbox.py       # LocalSandbox, SubprocessSandbox
├── memory/
│   └── session_store.py # JSONL persistence + compaction
├── extensions/
│   ├── mcp.py           # MCPBridge — connect MCP servers, register tools
│   └── observability.py # session_metrics() — timing, tokens, errors
└── transports/
    ├── cli.py           # Typer CLI (chat, run, tui, serve, …)
    ├── tui.py           # Rich TUI
    ├── web.py           # ASGI app + SSE streaming
    └── stream.py        # EventStream / AsyncEventStream
```

The core loop:

```python
while not done and step < max_steps:
    if cancel_event and cancel_event.is_set(): break   # cooperative cancel
    if elapsed > timeout: break

    t = time.monotonic()
    response = await provider.complete(messages, tools, system)
    response.meta.duration_ms = (time.monotonic() - t) * 1000  # provider timing

    if response.tool_calls:
        results = await tool_executor.execute(response.tool_calls)  # tool timing inside
        session.append(results)
        continue

    session.append(response)
    if response.stop_reason in {"end_turn", "max_tokens"}:
        done = True
```

## Testing

```bash
pip install "aar-agent[dev]"
pytest tests/ -v
```

The test suite (214 tests) runs entirely without live API calls using a `MockProvider`. Tests cover:

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
agent run "Reply with the word PONG." --provider ollama --model llama3.2
```

#### Anthropic

```bash
export ANTHROPIC_API_KEY=sk-ant-...
pytest tests/test_providers.py -m live --live -k Anthropic -v
```

Uses `claude-haiku-4-5-20251001` by default (cheapest model). Covers plain text, tool calls, stop-reason normalization, and provider meta. Quick smoke-test via CLI:

```bash
agent run "Reply with the word PONG." --provider anthropic --model claude-haiku-4-5-20251001
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
