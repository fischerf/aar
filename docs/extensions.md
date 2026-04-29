# Aar Extensions — Developer Guide

Extensions let you hook into every stage of the Aar agent lifecycle without touching
core code. An extension is a Python module that exposes a `register(api)` function —
Aar calls it once, you use the `api` handle to subscribe to events, register tools,
add slash-commands, and append to the system prompt. Extensions are auto-discovered
from installed packages, user-global files, or project-local files.

---

## Quick Start

Create `~/.aar/extensions/hello.py`:

```python
from agent.extensions.api import ExtensionAPI, ExtensionContext

def register(api: ExtensionAPI) -> None:
    @api.on("session_start")
    def on_start(event, ctx: ExtensionContext) -> None:
        ctx.logger.info("Hello from my first extension!")
```

That's it. Next time you run `aar chat`, the loader picks it up automatically.

---

## Extension Anatomy

Every extension must expose a **`register(api)`** function — sync or async. The
loader calls it once at startup and passes an `ExtensionAPI` handle. You use
decorators on that handle to wire everything up:

```python
from agent.extensions.api import ExtensionAPI, ExtensionContext, BlockResult

async def register(api: ExtensionAPI) -> None:
    # Subscribe to lifecycle events
    @api.on("tool_call")
    async def guard(event, ctx: ExtensionContext) -> BlockResult | None:
        if "rm -rf" in event.arguments.get("command", ""):
            return api.block("Dangerous command blocked")
        return None

    # Register a custom tool
    @api.tool(
        name="greet",
        description="Greet someone",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    )
    async def greet(name: str, ctx: ExtensionContext) -> str:
        return f"Hello, {name}!"

    # Register a slash-command
    @api.command("ping", description="Pong!")
    async def ping(args: str, ctx: ExtensionContext) -> None:
        ctx.logger.info("Pong!")

    # Append text to the system prompt
    api.append_system_prompt("You have access to a greeting tool.")
```

Both sync and async `register` functions are supported — the loader detects which
one you provided via `asyncio.iscoroutinefunction`.

---

## ExtensionAPI Reference

The `ExtensionAPI` handle is the only object your extension interacts with.

### `api.on(event: str)`

Decorator. Subscribe a handler to a lifecycle event (see [Event Hooks](#event-hooks)).

```python
@api.on("session_start")
def on_start(event, ctx: ExtensionContext) -> None:
    ...
```

### `api.tool(name, description, input_schema, *, side_effects=None, requires_approval=False)`

Decorator. Register a tool the LLM can call. The wrapped function becomes the
handler. Parameters:

| Parameter | Type | Description |
|---|---|---|
| `name` | `str` | Tool name (unique across all extensions) |
| `description` | `str` | Shown to the LLM in the tool list |
| `input_schema` | `dict` | JSON Schema for the tool's input |
| `side_effects` | `list[SideEffect]` | Optional; defaults to `[SideEffect.NONE]` |
| `requires_approval` | `bool` | If `True`, the safety layer prompts before execution |

### `api.register_tool(spec: ToolSpec)`

Imperative alternative to the `@api.tool` decorator — pass a pre-built `ToolSpec`.

### `api.command(name: str, *, description: str = "")`

Decorator. Register a `/name` slash-command available in the CLI and TUI.

### `api.append_system_prompt(text: str)`

Append text to the system prompt assembled for every turn. Call it multiple times
to add multiple paragraphs.

### `api.block(reason: str) -> BlockResult`

Static helper. Return a `BlockResult` from event handlers (e.g. `tool_call`) to
prevent the action from executing.

### `api.events -> ExtensionEventBus`

A per-extension pub/sub bus for inter-extension or internal communication:

```python
# Emit from anywhere in your extension
api.events.emit("my_ext:something_happened", {"key": "value"})

# Subscribe
@api.events.on("my_ext:something_happened")
def handle(payload):
    ...
```

The bus supports both sync and async handlers. Use `api.events.emit()` for
fire-and-forget, or `await api.events.emit_async()` to await async handlers.

---

## ExtensionContext

Every event handler and tool handler receives an `ExtensionContext`:

```python
@dataclass(frozen=True)
class ExtensionContext:
    session: Any            # Live Session object (read-only)
    config: Any             # AgentConfig — read, don't mutate
    signal: asyncio.Event   # Set when cancellation is requested
    logger: logging.Logger  # Scoped to "aar.ext.<name>"
```

| Field | Use for |
|---|---|
| `ctx.session` | Read step count, message history, token usage |
| `ctx.config` | Check user configuration values |
| `ctx.signal` | Cooperative cancellation — check `ctx.signal.is_set()` in long operations |
| `ctx.logger` | All output — scoped so logs show your extension's name |

---

## Event Hooks

Handlers are called in registration order. All handlers receive `(event, ctx)`.

| Event | Fires when | Handler may return |
|---|---|---|
| `session_start` | Session initialised | — |
| `session_end` | Session finishing | — |
| `before_turn` | Before each LLM request | `list[Message]` to override |
| `after_turn` | After each LLM response | — |
| `user_message` | User message received | `str` (transformed text) |
| `tool_call` | Before tool execution | `BlockResult` to prevent |
| `tool_result` | After tool execution | `str` (replacement output) |
| `assistant_message` | Complete assistant response | — |
| `stream_chunk` | Per-token streaming chunk | — |
| `error` | Error raised in the loop | — |

**Transform events (`user_message`, `tool_result`):** these are piped through
handlers sequentially — each handler receives the previous handler's output as its
input. The final transformed value is what the loop uses. This enables chaining
multiple extensions that each refine or enrich the content (e.g. one strips PII,
another injects context).

---

## Blocking Tool Calls

Return a `BlockResult` from a `tool_call` handler to prevent execution:

```python
from agent.extensions.api import ExtensionAPI, ExtensionContext, BlockResult

def register(api: ExtensionAPI) -> None:
    PROTECTED = {".env", "secrets.yaml", "id_rsa"}

    @api.on("tool_call")
    def protect_files(event, ctx: ExtensionContext) -> BlockResult | None:
        path = event.arguments.get("path", "")
        if any(path.endswith(p) for p in PROTECTED):
            return api.block(f"Write to {path} blocked by protected-paths extension")
        return None
```

When a handler returns `BlockResult`, the tool call is skipped and the reason is
surfaced to the LLM as an error message.

---

## Custom Tools

Tools are exposed to the LLM alongside built-in tools. Define `input_schema` as a
standard JSON Schema object:

```python
def register(api: ExtensionAPI) -> None:
    @api.tool(
        name="word_count",
        description="Count words in the given text",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to count words in"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    )
    def word_count(text: str, ctx: ExtensionContext) -> str:
        count = len(text.split())
        return f"{count} words"
```

For tools with side effects or that need approval:

```python
from agent.tools.schema import SideEffect

@api.tool(
    name="deploy",
    description="Deploy to production",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    side_effects=[SideEffect.NETWORK],
    requires_approval=True,
)
async def deploy(ctx: ExtensionContext) -> str:
    ...
```

---

## Slash-Commands

Register interactive commands available in the CLI and TUI with `/name`:

```python
def register(api: ExtensionAPI) -> None:
    @api.command("stats", description="Show session statistics")
    def stats(args: str, ctx: ExtensionContext) -> None:
        s = ctx.session
        ctx.logger.info("Steps: %d | Messages: %d", s.step_count, len(s.messages))

    @api.command("mood", description="Set companion mood")
    def mood(args: str, ctx: ExtensionContext) -> None:
        ctx.logger.info("Mood set to: %s", args.strip() or "neutral")
```

The `args` parameter receives everything after the command name (e.g. `/mood happy`
passes `"happy"`).

---

## Auto-Discovery

Extensions are discovered from three tiers. Higher tiers shadow lower ones by name.

| Priority | Location | Scope |
|---|---|---|
| 1 — Installed packages | `aar_extensions` entry-point group | Global (pip-installed) |
| 2 — User directory | `~/.aar/extensions/*.py` or `~/.aar/extensions/*/` | Per-user |
| 3 — Project directory | `.agent/extensions/*.py` or `.agent/extensions/*/` | Per-project |

**Shadowing:** if a project extension has the same name as an installed package
extension, the project version wins. This lets you override or develop locally.

Files and directories starting with `_` or `.` are ignored.

A package directory must contain an `__init__.py` with a `register` function.

---

## Publishing to PyPI

To distribute your extension, create a package with the `aar_extensions` entry-point
group.

**Naming convention:** `aar-ext-<slug>` (e.g. `aar-ext-git-checkpoint`).

Example `pyproject.toml`:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "aar-ext-my-extension"
version = "0.1.0"
description = "My awesome Aar extension"
requires-python = ">=3.11"
dependencies = ["aar"]
keywords = ["aar-extension"]

[project.entry-points."aar_extensions"]
my_extension = "aar_ext_my_extension:register"
```

The entry-point key (`my_extension`) becomes the extension name inside Aar. The
value points to the `register` function using `module.path:function` syntax.

After `pip install aar-ext-my-extension`, Aar discovers it automatically — no
config changes needed.

**Recommended package layout:**

```
aar-ext-my-extension/
├── pyproject.toml
├── README.md
└── aar_ext_my_extension/
    ├── __init__.py      # contains register(api)
    └── ...
```

---

## CLI Commands

Aar provides built-in CLI commands for managing extensions:

```bash
# Install an extension from PyPI
aar install aar-ext-permission-gate

# Install from a local path (development)
aar install ./my-extension/

# List all discovered extensions (all three tiers)
aar extensions list

# Inspect what an extension registers (hooks, tools, commands)
aar extensions inspect my_extension
```

`aar install` is a thin wrapper around `pip install` that validates the package
declares at least one `aar_extensions` entry point — it warns loudly if the
package exists but isn't an Aar extension.

`aar extensions list` shows a table with name, source tier (entrypoint / user /
project), and file path for every discovered extension.

`aar extensions inspect <name>` loads the extension and displays its registered
event hooks, tools (with descriptions), slash-commands, and system prompt
additions.

---

## Example: Companion Extension

The built-in companion extension at `agent/extensions/contrib/companion.py`
demonstrates a real-world extension pattern. Here's a condensed walkthrough:

```python
from agent.extensions.api import ExtensionAPI, ExtensionContext
from agent.transports.companion_state import CompanionEngine, xp_fraction

def register(api: ExtensionAPI) -> None:
    engine: CompanionEngine | None = None

    # Initialise state on session start
    @api.on("session_start")
    def _on_session_start(event, ctx: ExtensionContext) -> None:
        nonlocal engine
        engine = CompanionEngine()
        if ctx.session is not None:
            engine.bootstrap_from_session(ctx.session)

    # Track progress on every tool call
    @api.on("tool_call")
    def _on_tool_call(event, ctx: ExtensionContext) -> None:
        if engine is None:
            return
        levelled_up = engine.on_step()
        if levelled_up:
            ctx.logger.info("Level up! Now level %d", engine.level)

    # Expose state as a tool the LLM can query
    @api.tool(
        name="companion_status",
        description="Return the companion's mood, level, and XP",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    def _companion_status(ctx: ExtensionContext) -> str:
        if engine is None:
            return "companion not initialised"
        xp = xp_fraction(engine.steps, engine.level)
        return f"mood={engine.mood.value} level={engine.level} xp={xp*100:.0f}%"
```

Key patterns to note:

- **Closure state** — `engine` is captured via `nonlocal`; no globals needed.
- **Graceful nil checks** — handlers bail early if `engine is None`.
- **Event-driven** — mood transitions happen reactively via `tool_call`, `stream_chunk`, `error`.
- **Tool as a read window** — `companion_status` gives the LLM (or user) visibility into extension state without mutation.