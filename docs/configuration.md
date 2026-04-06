# Configuration

Aar is configured via `AgentConfig` — either in code or through a JSON config file.

## AgentConfig reference

```python
from agent import AgentConfig, ProviderConfig, SafetyConfig, ToolConfig
from agent.core.config import TUIConfig

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
    tui=TUIConfig(
        theme="default",                               # "default" | "claude" | "decker" or custom name
        layout={},                                     # section visibility (see docs/themes.md)
    ),
    session_dir=".agent/sessions",
    shell_path="",                                 # custom shell binary (see below)
    project_rules_dir=".agent",                    # project rules folder (see below)
    log_level="WARNING",                           # DEBUG | INFO | WARNING | ERROR | CRITICAL
)
```

## Config loading and precedence

All CLI modes and the web transport load configuration from multiple sources. The order of precedence (highest wins):

| Source | `aar chat` / `aar run` / `aar tui` | `aar serve` | `WebTransport()` programmatic | `Agent()` programmatic |
|--------|:----------------------------------:|:-----------:|:-----------------------------:|:----------------------:|
| Explicit CLI flag | highest | yes (fewer flags — see [Web API](web-api.md)) | — | — |
| `--config <file>` | yes | yes | — | — |
| `~/.aar/config.json` | auto-discovered | auto-discovered | auto-discovered | not loaded |
| Built-in defaults | lowest | yes | yes | only source unless you pass `config=` |

When using `Agent()` directly in code, the config file is **not** loaded automatically — pass a config explicitly if you need it:

```python
from pathlib import Path
from agent.core.config import load_config
from agent import Agent

config = load_config(Path("~/.aar/config.json").expanduser())
agent = Agent(config=config)
```

## Approval behaviour by mode

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

## Log level

Control how much the agent logs to stderr. The default is `WARNING`.

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

## Configurable system prompt

By default, the system prompt is assembled automatically from up to three layers:

| Layer | Source | Purpose |
|-------|--------|---------|
| **Base** | built-in | Runtime facts — OS, working directory, shell |
| **Global rules** | `~/.aar/rules.md` | Personal preferences that apply to all projects |
| **Project rules** | `<project_rules_dir>/rules.md` | Project-specific instructions (checked into git) |

Each layer is optional. If no rules files exist, only the base prompt is used. When present, the layers are concatenated in order, separated by `---`.

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

**Override** — if you pass `system_prompt` explicitly to `AgentConfig`, the auto-assembly is skipped entirely and your string is used as-is.

## Configurable shell

By default, Aar uses Git Bash (`bash -c`) on Windows and the system shell (`/bin/sh`) on Unix for tool execution. Override this with `shell_path`:

**Via config file:**

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

## Configurable project rules directory

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

## TUI theme and layout

The `tui` section controls the TUI's visual appearance and section visibility. See [Themes & Layout](themes.md) for full details.

**Via config file:**

```json
{
  "tui": {
    "theme": "claude",
    "layout": {
      "reasoning": { "visible": false },
      "token_usage": { "visible": false }
    }
  }
}
```

**Via CLI flag** (theme and mode):

```bash
aar tui --theme decker
aar tui -t claude
aar tui --fixed                 # full-screen mode with fixed bars, scrollable body, mouse support
aar tui --fixed --theme decker  # fixed mode with a specific theme
```

Fixed mode includes keyboard shortcuts: **Ctrl+T** (cycle theme), **Ctrl+K** (toggle thinking), **Ctrl+L** (clear), **Ctrl+Y** (copy block), **↑/↓** (input history). See [Themes & Layout](themes.md) for the full list.

**At runtime** (inside the TUI):

```
/theme              # list available themes
/theme claude       # switch theme
/theme next         # cycle themes
```

Built-in themes: `default`, `claude`, `decker`. Custom themes go in `~/.aar/themes/<name>.json` — run `aar init` to get a template and JSON schema.
