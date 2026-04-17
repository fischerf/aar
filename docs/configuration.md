# Configuration

Aar is configured via `AgentConfig` — either in code or through a JSON config file.

## AgentConfig reference

```python
from agent import AgentConfig, GuardrailsConfig, ProviderConfig, SafetyConfig, ToolConfig
from agent.core.config import SandboxConfig, TUIConfig

config = AgentConfig(
    provider=ProviderConfig(
        name="anthropic",                          # "anthropic" | "openai" | "ollama" | "generic"
        model="claude-sonnet-4-20250514",
        api_key="...",                             # or set via env var
        max_tokens=4096,
        temperature=0.0,
        response_format="",                        # "" | "json" | "json_schema"
        json_schema={},                            # schema when response_format="json_schema"
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
        sandbox=SandboxConfig(                     # see docs/safety.md for all modes and per-mode options
            mode="local",                          # "local" | "subprocess" | "workspace" | "windows" | "wsl" | "auto"
        ),
    ),
    guardrails=GuardrailsConfig(
        max_tokens_recoveries=2,                   # retry after output truncation (0 = disabled)
        max_repeated_tool_steps=3,                  # stop after N identical tool-call patterns
        reserve_tokens=512,                         # budget proximity threshold
        reserve_cost_fraction=0.1,                  # cost proximity fraction
    ),
    max_steps=50,
    max_retries=3,                                 # provider request retry attempts
    timeout=300.0,                                 # seconds
    streaming=False,                               # use token-level streaming when supported
    context_window=0,                              # model context limit in tokens; 0 = no management
    context_strategy="sliding_window",             # "sliding_window" | "none"
    system_prompt="You are a helpful assistant.",
    tui=TUIConfig(
        theme="default",                               # "default" | "contrast" | "decker" | "sleek" or custom name
        layout={},                                     # section visibility (see docs/themes.md)
    ),
    token_budget=0,                                # max total tokens per run; 0 = unlimited
    cost_limit=0.0,                                # max USD cost per run; 0.0 = unlimited
    token_warning_threshold=0.8,                   # TUI warning at 80% of budget
    cost_warning_threshold=0.8,                    # TUI warning at 80% of cost limit
    session_dir=".agent/sessions",
    project_rules_dir=".agent",                    # project rules folder (see below)
    log_level="WARNING",                           # DEBUG | INFO | WARNING | ERROR | CRITICAL
    log_file=None,                                 # opt-in file logging path (append mode)
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
aar serve --log-level DEBUG
```

### Log file

By default aar logs to stderr only (12-factor / container-friendly). Opt in to file logging
with `log_file` in the config or `--log-file` on the CLI:

```json
{
  "log_level": "DEBUG",
  "log_file": "/var/log/aar/agent.log"
}
```

```bash
aar serve --log-level INFO --log-file /var/log/aar/agent.log
aar chat --log-file ./debug.log
```

The file handler uses append mode and includes timestamps. Both stderr and file handlers are
active when `log_file` is set.

## Token budget & cost limits

You can cap how many tokens or how much estimated cost a single agent run is allowed to consume. Both limits default to zero (unlimited).

### How token tracking works

Token counts are read from the `ProviderMeta` event that fires after every provider call — for both streaming and non-streaming responses. In streaming mode the final chunk from the provider carries the usage data; `_consume_stream()` (in `agent/core/provider_runner.py`) captures it and attaches it to the response before the event is emitted. See [Tokens, costs, and budgets](tokens.md) for the full pipeline, per-provider details, and how each transport displays the counts.

### How cost estimation works

Run `aar init` to get `~/.aar/pricing.template.json` — a copy of the full built-in pricing table — as a reference. Rename to pricing.json and adjust if needed.

Aar loads a built-in pricing table from `agent/core/pricing.json` (shipped with the package). If `~/.aar/pricing.json` exists it is merged on top, letting you extend or override any entry. After each provider call the framework multiplies token counts by the matching per-token price to produce an estimated USD cost. The estimate is **approximate** — prompt-caching discounts, batching, and future price changes are not reflected.

- Cost is accumulated across all steps in the run, just like tokens.
- When `cost_limit` (if > 0) is exceeded the agent stops the same way as for `token_budget`.
- Local or Ollama models that don't match any pricing-table entry will report **$0.00** cost.

To add prices for custom or local models (e.g. Ollama), create or edit `~/.aar/pricing.json`:

```json
{
  "_comment": "USD per 1M tokens. Keys are model-name prefixes.",
  "gemma4": { "input_per_million": 0.05, "output_per_million": 0.10, "cache_read_per_million": 0.0, "cache_write_per_million": 0.0 }
}
```

### Warning thresholds (TUI only)

`token_warning_threshold` and `cost_warning_threshold` are fractions (0.0–1.0) of the corresponding limit. When the running total crosses the threshold the TUI switches the counter display to **red**. This is a visual cue only — the agent keeps running until the hard limit is hit.

### Enforcement details

| Behaviour | Detail |
|-----------|--------|
| Checked | After each provider call, before the next step |
| Scope | Per-run (resets when `Agent.run()` is called again) |
| State on exceed | `AgentState.BUDGET_EXCEEDED` |
| Event emitted | `ErrorEvent` with a descriptive message |
| Warning thresholds | Visual only — TUI counter turns red |

### Configuration examples

**Via `AgentConfig` in code:**

```python
config = AgentConfig(
    token_budget=100_000,          # stop after 100 k tokens
    cost_limit=5.0,                # stop after ~$5
    token_warning_threshold=0.9,   # yellow → red at 90 %
    cost_warning_threshold=0.9,
)
```

**Via config file** (`~/.aar/config.json` or `--config`):

```json
{
  "token_budget": 100000,
  "cost_limit": 5.0,
  "token_warning_threshold": 0.9,
  "cost_warning_threshold": 0.9
}
```

> For full details on how token counts flow through the system, how each transport displays them, and per-provider caveats, see **[Tokens, costs, and budgets](tokens.md)**.

## Runtime guardrails

`GuardrailsConfig` provides mechanical safety nets for the agent loop — things that cannot be expressed as system prompt instructions.

| Field | Default | Meaning |
|-------|---------|---------|
| `max_tokens_recoveries` | `2` | How many times the loop retries after output truncation (`max_tokens`). Set to `0` to disable. |
| `max_repeated_tool_steps` | `3` | Stop the loop when the same tool-call pattern repeats this many times in a row. |
| `reserve_tokens` | `512` | Token budget proximity threshold — the loop reports "near budget" below this margin. |
| `reserve_cost_fraction` | `0.1` | Cost proximity — fraction of `cost_limit` that triggers "near budget". |

The guardrails are deliberately minimal. Agent behavior (planning, persistence, completion quality) is guided entirely by the system prompt — see the `rules.md` file loaded via the configurable system prompt layers.

## Configurable system prompt

By default, the system prompt is assembled automatically from up to five layers (all optional except Base):

| # | Layer | Source | Purpose |
|---|-------|--------|---------|
| 1 | **Base** | built-in | Runtime facts — OS, working directory, shell |
| 2 | **Global rules** | `~/.aar/rules.md` | Personal preferences that apply to all projects |
| 3 | **Global drop-ins** | `~/.aar/rules.d/*.md` (sorted) | Environment-specific additions; drop files in without editing the main file |
| 4 | **Project rules** | `<project_rules_dir>/rules.md` | Project-specific instructions (checked into git) |
| 5 | **Project drop-ins** | `<project_rules_dir>/rules.d/*.md` (sorted) | Per-contributor or per-machine overrides; can be gitignored |

If no rules files exist, only the base prompt is used. When present, the layers are concatenated in order, separated by `---`.

**Global rules** — create `~/.aar/rules.md` for preferences that follow you across projects:

```markdown
# My rules
- Always use type hints on public functions.
- Prefer pathlib over os.path.
- Use ruff for formatting.
```

**Global drop-ins** — place any number of `.md` files in `~/.aar/rules.d/` and they are appended after `rules.md`, sorted by filename. Useful for environment-specific rules (e.g. `10-work-proxy.md`, `20-local-models.md`) without touching the main file.

**Project rules** — create `<project_rules_dir>/rules.md` (default `.agent/rules.md`) for instructions specific to the current repo:

```markdown
# Project rules
- This is a FastAPI app. Use pytest-asyncio for async tests.
- Follow the existing service pattern in app/services/.
```

**Project drop-ins** — place `.md` files in `<project_rules_dir>/rules.d/` for per-contributor or per-machine additions. Add `rules.d/` to `.gitignore` if you don't want them committed, or commit them for shared team overrides.

Run `aar init` to create the skeleton files and directories (`rules.md`, `rules.d/`) for both global and project layers.

**Override** — if you pass `system_prompt` explicitly to `AgentConfig`, the auto-assembly is skipped entirely and your string is used as-is.

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
    "theme": "default",
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
aar tui -t contrast
aar tui --fixed                 # full-screen mode with fixed bars, scrollable body, mouse support
aar tui --fixed --theme decker  # fixed mode with a specific theme
```

Fixed mode includes keyboard shortcuts: **Ctrl+S** (send), **Ctrl+X** (cancel agent), **Ctrl+T** (cycle theme), **Ctrl+K** (toggle thinking), **Ctrl+L** (clear), **Ctrl+G** (log viewer), **Ctrl+Q** (quit), **Ctrl+Up/Down** (input history), **Page Up/Down** (scroll). Enter adds a new line; Ctrl+S submits. See [Themes & Layout](themes.md) for the full reference.

**At runtime** (inside the TUI):

```
/theme              # list available themes
/theme decker       # switch theme
/theme next         # cycle themes
```

Built-in themes: `default`, `contrast`, `decker`, `sleek`. Custom themes go in `~/.aar/themes/<name>.json` — run `aar init` to get a template and JSON schema.


