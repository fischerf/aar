# EPA — Extensible Python Agent

A lean, provider-agnostic agent framework with a thin core loop, typed event model, sandboxed tool execution, and pluggable transports.

## Design goals

- **Thin core loop** — the main execution path is ~80 lines and readable at a glance
- **Typed event model** — every message, tool call, and result is a typed, serializable event
- **Provider-agnostic** — swap between Anthropic, OpenAI, and Ollama without changing agent code
- **Safe by default** — path restrictions, command deny-lists, and approval gates built in
- **Modular transports** — the same agent runs from CLI, TUI, web API, or embedded in your code
- **Persistent sessions** — every run is saved as JSONL and resumable

## Installation

```bash
# Core only (no LLM provider)
pip install epa-agent

# With a specific provider
pip install "epa-agent[anthropic]"
pip install "epa-agent[openai]"
pip install "epa-agent[ollama]"   # Ollama uses httpx, already a core dep

# Development
pip install "epa-agent[anthropic,dev]"
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

Sessions are automatically saved as JSONL files. Every event (messages, tool calls, results, metadata) is persisted.

```python
from agent import Agent
from agent.memory.session_store import SessionStore

agent = Agent()
store = SessionStore(".agent/sessions")

# First run
session = await agent.run("Write a Python script that sorts a CSV")
store.save(session)
print(session.session_id)  # e.g. "a3f1b2c4d5e6f7a8"

# Resume later
session = store.load("a3f1b2c4d5e6f7a8")
session = await agent.run("Now add error handling", session=session)

# List all sessions
print(store.list_sessions())
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
│   └── session_store.py # JSONL persistence
└── transports/
    ├── cli.py           # Typer CLI (chat, run, tui, serve, …)
    ├── tui.py           # Rich TUI
    ├── web.py           # ASGI app + SSE streaming
    └── stream.py        # EventStream / AsyncEventStream
```

The core loop:

```python
while not done and step < max_steps:
    if elapsed > timeout: break

    response = await provider.complete(messages, tools, system)

    if response.tool_calls:
        results = await tool_executor.execute(response.tool_calls)
        session.append(results)
        continue

    session.append(response)
    if response.stop_reason in {"end_turn", "max_tokens"}:
        done = True
```

## Testing

```bash
pip install "epa-agent[dev]"
pytest tests/ -v
```

The test suite (139 tests) runs entirely without live API calls using a `MockProvider`. Tests cover:

- Loop termination, max steps, timeout, provider errors
- Session persistence, resumption, message conversion
- Event serialization round-trips for all event types
- Provider normalization for Anthropic, OpenAI, and Ollama (mocked)
- Tool registry, schema inference, execution (sync/async, timeout, truncation)
- Safety policy (command deny-list, path restrictions, read-only mode, approval gates)
- Sandbox execution and timeout

## Requirements

- Python 3.11+
- `pydantic >= 2.0`
- `httpx >= 0.27`
- `typer >= 0.12`
- `rich >= 13.0`
- Provider SDK as needed: `anthropic`, `openai`
