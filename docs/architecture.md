# Architecture

Aar is a lean, provider-agnostic agent framework. This document explains how the pieces fit together.

## Principles

1. **Thin core loop** — the main execution path (`loop.py`) is under 80 lines. It does exactly three things: call the provider, execute tool calls, and append events to the session.
2. **Typed event model** — every interaction (messages, tool calls, results, metadata) is a Pydantic model. Events are serializable, inspectable, and carry timing data.
3. **Provider-agnostic** — the agent loop works with any provider that implements the `Provider` ABC. Swapping between Anthropic, OpenAI, Ollama, or a generic endpoint requires changing one config field.
4. **Safe by default** — path restrictions, command deny-lists, and approval gates are built in and always active. Interactive modes enable a workspace sandbox by default.
5. **Modular transports** — the same `Agent` class runs from CLI, TUI, web API, or embedded in your code. Transports only handle I/O; they never contain business logic.

## Component overview

```
agent/
├── core/           # The heart: loop, agent, events, session, config
├── providers/      # LLM API adapters (Anthropic, OpenAI, Ollama, Generic)
├── tools/          # Tool registry, schema, execution engine
├── safety/         # Policy engine, permission manager, sandboxes
├── memory/         # Session persistence (JSONL)
├── extensions/     # MCP bridge, observability
└── transports/     # CLI, TUI, web, event stream
    ├── themes/     # Theme models, built-in themes, registry
    ├── tui_utils/  # Shared formatting helpers for TUI transports
    └── tui_widgets/  # Textual widget classes (bars, blocks, input, chat body)
```

## Core loop

The agent loop lives in `agent/core/loop.py`. It runs until the provider signals completion, a step limit is reached, a timeout fires, or cancellation is requested.

```
while not done and step < max_steps:
    if cancel_event.is_set(): break
    if elapsed > timeout: break

    # streaming path (streaming: true)
    async for delta in provider.stream(messages, tools, system):
        emit(StreamChunk)       # text delta or reasoning delta
        # final delta carries usage counts
    emit(ProviderMeta)          # timing + token usage (after stream closes)

    # — or non-streaming path (streaming: false, the default) —
    response = await provider.complete(messages, tools, system)
    emit(ProviderMeta)          # timing + token usage

    for tool_call in response.tool_calls:
        emit(ToolCall)          # before execution
        result = await executor.execute_one(tool_call)
        emit(ToolResult)        # after execution

    if response.text:
        emit(AssistantMessage)  # after all tool calls in this step

    if response.stop_reason in {"end_turn", "max_tokens"}:
        done = True
```

**Event emission order matters:** `ToolCall` events are emitted *before* the `AssistantMessage` in the same step. This allows `session.to_messages()` to bundle `tool_use` blocks into the assistant message for the next provider call, matching the Anthropic/OpenAI message format.

**Token counts** arrive via the `ProviderMeta` event in both paths. For streaming responses, `_consume_stream()` captures the usage data from the provider's final done-chunk and attaches it to the `ProviderResponse` before the event is emitted. This means the counts are always available on the same `ProviderMeta` event regardless of whether streaming is enabled. See [Tokens, costs, and budgets](tokens.md) for the full pipeline.

### Session and messages

`Session` (`agent/core/session.py`) holds the full event history. `session.to_messages()` converts the event stream into the provider-neutral message format:

- Pending `ToolCall` events are bundled as `tool_use` content blocks in the assistant message
- `ToolResult` events are flushed as `tool_result` content blocks in a user message
- This matches the Anthropic API's expected message structure (`assistant[text+tool_use] -> user[tool_result]`)

### State machine

```
IDLE → RUNNING → COMPLETED
                → CANCELLED
                → ERROR
                → MAX_STEPS
                → TIMED_OUT
```

State transitions are managed by the loop. The final state is set on the session before returning.

## Providers

All providers implement the `Provider` ABC (`agent/providers/base.py`):

```python
class Provider(ABC):
    async def complete(self, messages, tools, system_prompt) -> ProviderResponse
    def capabilities(self) -> ProviderCapabilities
```

`ProviderResponse` is a normalized container with: `text`, `tool_calls`, `stop_reason`, `meta` (timing + usage), and optional `reasoning_blocks`.

| Provider | Module | SDK | Features |
|----------|--------|-----|----------|
| Anthropic | `anthropic.py` | `anthropic` | Tools, streaming, extended thinking |
| OpenAI | `openai.py` | `openai` | Tools, streaming, Azure/Together via `base_url` |
| Ollama | `ollama.py` | `httpx` | Tools, reasoning extraction (`deepseek-r1`) |
| Generic | `generic.py` | `httpx` | Tools, streaming, any OpenAI-compatible endpoint |

Provider selection is config-driven:

```python
ProviderConfig(name="anthropic", model="claude-sonnet-4-6")
```

The `PROVIDER_REGISTRY` in `agent.py` maps names to classes via lazy import.

## Tool system

### Registry

`ToolRegistry` (`agent/tools/registry.py`) holds all available tools. Tools can be registered via:

- **Decorator**: `@registry.register(name="...", description="...", side_effects=[...])`
- **Explicit**: `registry.add(ToolSpec(...))`
- **MCP bridge**: `bridge.register_all(registry)` — registers all tools from connected MCP servers

Each tool is a `ToolSpec` with: name, description, input JSON schema, side-effects, and a handler function.

### Side effects

Every tool declares its side effects:

| Side effect | Meaning |
|-------------|---------|
| `READ` | Reads files or data |
| `WRITE` | Modifies files or state |
| `EXECUTE` | Runs a shell command |
| `NETWORK` | Makes network requests |
| `EXTERNAL` | Interacts with external services |

Side effects drive policy decisions (read-only mode blocks WRITE+EXECUTE, approval gates check WRITE or EXECUTE).

### Built-in tools

| Tool | Side effects | Source |
|------|-------------|--------|
| `read_file` | READ | `tools/builtin/filesystem.py` |
| `write_file` | WRITE | `tools/builtin/filesystem.py` |
| `edit_file` | WRITE | `tools/builtin/filesystem.py` |
| `list_directory` | READ | `tools/builtin/filesystem.py` |
| `bash` | EXECUTE | `tools/builtin/shell.py` |

Built-ins are opt-in via `ToolConfig.enabled_builtins`. The agent constructor registers only the enabled set.

### Execution pipeline

`ToolExecutor` (`agent/tools/execution.py`) is the single entry point for all tool execution:

```
ToolCall → SafetyPolicy.check_tool() → ALLOW → sandbox.run() → ToolResult
                                      → DENY  → error ToolResult
                                      → ASK   → ApprovalCallback → approve/deny
```

The executor wraps results with timing (`duration_ms`) and enforces output truncation (`max_output_chars`).

## Safety

See [`docs/safety.md`](safety.md) for the full safety reference.

Three components:

- **SafetyPolicy** (`safety/policy.py`) — evaluates tool calls against declared rules, returns ALLOW/DENY/ASK
- **PermissionManager** (`safety/permissions.py`) — handles ASK decisions via the approval callback, caches APPROVED_ALWAYS
- **Sandbox** (`safety/sandbox.py`) — controls how shell commands are executed

### Workspace sandbox

Interactive transports (`chat`, `tui`) enable a two-layer sandbox by default:

1. `allowed_paths = [cwd/**]` — file tools can only access the current directory
2. `require_approval_for_execute = True` — bash commands require human approval

This works because `allowed_paths` restricts file tools but not bash (which can run arbitrary commands), and the approval gate covers bash separately.

## Sandboxing

Two sandbox implementations:

| Sandbox | How it works |
|---------|-------------|
| `LocalSandbox` | Direct `asyncio.create_subprocess_exec` — no isolation |
| `SubprocessSandbox` | Adds `ulimit` memory limits, restricted environment variables, timeout enforcement |

The sandbox is selected by `SafetyConfig.sandbox` (`"local"` or `"subprocess"`). Both return stdout+stderr as a string, capped at `ToolConfig.max_output_chars`.

## Event model

All events extend `Event` (`agent/core/events.py`) and carry a `type` field from `EventType`:

| Event class | Type | Key fields |
|-------------|------|-----------|
| `AssistantMessage` | `assistant_message` | `content` |
| `ToolCall` | `tool_call` | `tool_name`, `arguments`, `call_id` |
| `ToolResult` | `tool_result` | `tool_name`, `output`, `is_error`, `duration_ms` |
| `ReasoningBlock` | `reasoning` | `content` |
| `ProviderMeta` | `provider_meta` | `usage`, `duration_ms`, `model`, `provider` |
| `ErrorEvent` | `error` | `message` |
| `SessionEvent` | `session` | `action` |

Events are Pydantic models — fully serializable and type-safe. Subscribe with `agent.on_event(callback)`.

## Session persistence

`SessionStore` (`agent/memory/session_store.py`) saves sessions as JSONL files:

- One JSON line per event
- Session metadata (ID, state, step count) in the first line
- Resumable: load a session, pass it to `agent.run()`, continue where you left off
- Compactable: `store.compact(session_id, max_events=200)` trims old events

Each session carries:
- `session_id` — stable identifier
- `run_id` — refreshed on each `agent.run()` call
- `trace_id` — stable for the lifetime of the session object (for distributed tracing)

## Transports

Transports are thin I/O adapters. They create an `Agent`, wire up event handlers, and manage the user interaction loop.

| Transport | Module | Entry point | Notes |
|-----------|--------|-------------|-------|
| CLI | `transports/cli.py` | `aar chat`, `aar run`, etc. | Typer app, terminal approval callback |
| TUI | `transports/tui.py` | `aar tui` | Rich inline TUI, scrollable terminal UI |
| TUI Fixed | `transports/tui_fixed.py` | `aar tui --fixed` | Textual full-screen TUI with fixed header/footer |
| Web | `transports/web.py` | `aar serve` | ASGI app, SSE streaming, per-request safety override |
| Stream | `transports/stream.py` | (internal) | `EventStream` / `AsyncEventStream` for pub/sub |

Shared TUI sub-packages:

| Package | Contents |
|---------|----------|
| `transports/tui_utils/` | Formatting helpers shared by both TUI transports |
| `transports/tui_widgets/` | Textual widget classes: bars, blocks, chat body, input |
| `transports/themes/` | Theme models, built-in themes, theme registry |

All transports share the same `AgentConfig` schema. Transport-specific behavior is limited to:
- How user input is collected
- How events are displayed
- The approval callback implementation (terminal prompt vs. auto-deny vs. custom)

## MCP (Model Context Protocol)

`MCPBridge` (`agent/extensions/mcp.py`) connects to external MCP servers and registers their tools as native `ToolSpec` entries in the registry. The core loop sees MCP tools identically to built-in tools.

- Supports `stdio` (local subprocess) and `http` (Streamable HTTP) transports
- Connections stay alive for the full session lifetime
- Tool name collisions are caught eagerly; `prefix_tools=True` namespaces them

## Observability

`session_metrics()` (`agent/extensions/observability.py`) reads a session's events and returns:

- Total steps, tokens (input/output), provider duration, tool duration, tool calls, errors
- Per-step breakdown with the same metrics

No live provider or executor needed — it reads the event history only.
