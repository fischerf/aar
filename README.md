<div align="center">
<pre>
 █████╗  █████╗ ██████╗
██╔══██╗██╔══██╗██╔══██╗
███████║███████║██████╔╝
██╔══██║██╔══██║██╔══██╗
██║  ██║██║  ██║██║  ██║
╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝
</pre>

**Adaptive Action & Reasoning Agent**

[![Website](https://img.shields.io/badge/website-fischerf.github.io%2Faar-blue?style=flat-square)](https://fischerf.github.io/aar/)

</div>

A lean, provider-agnostic agent framework with a thin core loop, typed event model, sandboxed tool execution, and pluggable transports.

<table width="100%">
  <tr>
    <td width="50%" align="center" valign="top">
      <img src="https://raw.githubusercontent.com/fischerf/fischerf.github.io/07d6318c4b304f44e67e228588165eb6f9f2f5b3/aar/aar.gif" alt="AAR Agent — TUI" width="100%" />
      <br/><sub><b>AAR Agent with Textual interface (TUI)</b></sub>
    </td>
    <td width="50%" align="center" valign="top">
      <img src="https://raw.githubusercontent.com/fischerf/fischerf.github.io/a2f9fc10189fceeeb1a55f990248210bedfb06a8/aar/aar_acp.png" alt="Zed Editor — running AAR Agent via ACP" width="100%" />
      <br/><sub><b>Zed Editor — running AAR Agent via ACP</b></sub>
    </td>
  </tr>
</table>

## Design goals

- **Thin core loop** — the main execution path is small and readable at a glance
- **Typed event model** — every message, tool call, and result is a typed, serializable event
- **Provider-agnostic** — swap between Anthropic, OpenAI, Ollama, or any OpenAI-compatible endpoint without changing agent code
- **Safe by default** — path restrictions, command deny-lists, and approval gates built in
- **Modular transports** — the same agent runs from CLI, TUI, web API, or embedded in your code
- **Persistent sessions** — every run is saved as JSONL and resumable
- **Observable** — every provider call and tool execution is timed; sessions carry a `trace_id`
- **Cost-aware** — live token and cost tracking with configurable budget limits and visual warnings
- **Cancellable** — cooperative and hard cancellation built in

### Transport modes

| Command | Use case | Description |
|---------|----------|-------------|
| `aar run "…"` | Automation / CI | One-shot task — runs to completion and exits; no interaction |
| `aar chat` | Interactive CLI | Conversational loop in the terminal with approval prompts |
| `aar tui` | Interactive TUI | Scrollable Rich interface with live token counters |
| `aar tui --fixed` | Interactive TUI | Full-screen Textual UI with fixed header/footer bars, mouse support |
| `aar serve` | Remote / web | HTTP/SSE web API — use from a browser, curl, or remote agents |
| `aar acp` | IDE integration | [ACP](https://agentcommunicationprotocol.dev) stdio agent for Zed and other ACP-compatible editors |
| `aar acp --http` | Remote ACP | ACP over HTTP/SSE for programmatic or remote ACP clients |

## Installation

> **Note:** `aar-agent` is not published to PyPI.
> Use the **from-source install** below.

### Installing from source

```bash
git clone https://github.com/fischerf/aar.git
cd aar

# Everything at once (CLI,TUI,MCP,ACP,Providers)
pip install "aar-agent[all,dev]"

# or Full dev setup
pip install -e ".[all,dev]"

# Verify
aar --help
pytest tests/ -v
```

The `-e` flag creates a live link — editing files under `agent/` is reflected instantly without reinstalling.

## Quick start

Set `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or point `base_url` at a local Ollama instance.

## Usage

```bash
# Type to show help screen
> aar

# Show tui specific help
> aar tui --help

# Full-screen TUI with fixed bars, scrollable body, mouse support (like Claude Code/Codex but using Python)
> aar tui --fixed
> aar tui --fixed --theme decker

# Continous scrolling TUI
> aar tui

# Resume a previous session in TUI
> aar tui --session <session-id>

# Interactive chat (asks before write/execute, file tools restricted to cwd)
> aar chat --provider ollama --model llama3

# Disable the workspace sandbox for full access and load config from a JSON file
> aar chat --no-require-approval --no-restrict-to-cwd --config aar.json

# One-shot task
> aar run "Refactor main.py to use async/await"

# Skip approval prompts for scripted / CI use
> aar run --no-require-approval "Refactor main.py to use async/await"

# Start the HTTP/SSE web server
> aar serve --host 0.0.0.0 --port 8080
```

## ACP — IDE integration

`aar acp` starts an [Agent Communication Protocol](https://agentcommunicationprotocol.dev) agent that editors like [Zed](https://zed.dev) connect to over stdio.

```bash
aar acp              # stdio — for Zed and other ACP-compatible editors
aar acp --http       # HTTP/SSE — for remote or programmatic ACP clients
```

**Zed local dev** — add to `~/.config/zed/settings.json`:

```json
{
  "agent_servers": {
    "Aar": {
      "type": "custom",
      "command": "aar",
      "args": ["acp"],
      "env": {}
    }
  }
}
```

See [`docs/acp.md`](docs/acp.md) for the full setup guide, HTTP endpoint reference, and programmatic embedding.

## Architecture

```
agent/
├── core/           # Loop, agent, events, session, config
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

See [`docs/architecture.md`](docs/architecture.md) for a detailed walkthrough.

## Requirements

- Python 3.11+
- `pydantic >= 2.0`
- `httpx >= 0.27`
- `typer >= 0.12`
- `rich >= 13.0`
- Provider SDK as needed: `anthropic`, `openai`

### Windows — `bash` tool

The `bash` built-in tool requires a Unix-compatible shell. **Either** is sufficient:

| Option | Install | Notes |
|--------|---------|-------|
| **Git for Windows** (Git Bash) | [git-scm.com](https://git-scm.com/download/win) | Lightweight; adds `bash` to `PATH` |
| **WSL** | `wsl --install` in an admin terminal | Full Linux environment |

> **If both are installed**, WSL's `bash.exe` is found first by Windows `CreateProcess`, so WSL's bash will run. Keep this in mind for file path references.

```
Neither is required if you do not enable the `bash` built-in tool.
```

```
"NOTE: On Windows with WSL, 'python' in bash may resolve to WSL's Python (a separate env). "
"To install packages that will be visible to bash-executed scripts, use the pip_install tool "
"or run: bash('python -m pip install <package>'). "
```

## Documentation

| Document | Contents |
|----------|----------|
| [Configuration](docs/configuration.md) | `AgentConfig` reference, config precedence, approval modes, logging, system prompt, shell, project rules |
| [Tokens & Cost](docs/tokens.md) | Token tracking pipeline, budget enforcement, cost estimation, pricing tables, TUI display |
| [ACP](docs/acp.md) | ACP stdio setup for Zed and other editors, HTTP/SSE mode, programmatic embedding, endpoint reference |
| [Providers](docs/providers.md) | Anthropic, OpenAI, Ollama, Generic setup and options |
| [Safety](docs/safety.md) | Deny lists, path restrictions, sandbox modes, approval callbacks |
| [MCP](docs/mcp.md) | MCP host integration — CLI config, programmatic API, transports, reference tables |
| [Web API](docs/web-api.md) | HTTP endpoints, SSE streaming, ASGI embedding, per-request safety |
| [Themes & Layout](docs/themes.md) | Built-in themes, custom themes, layout sections, full-screen fixed-bar mode, keyboard shortcut reference |
| [Development](docs/development.md) | Programmatic usage, image input, custom tools, events, sessions, cancellation, observability, testing |
| [Architecture](docs/architecture.md) | Component walkthrough, core loop, event flow, provider internals |
| [Agent Loop & Guardrails](docs/agent_loop.md) | Core loop flow diagram, guardrail mechanics, state transitions, config tuning |
| [Prompting](docs/prompting.md) | System prompt design, provider-specific tips, tool guidance |

---

## License

Apache License 2.0
