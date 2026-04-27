[![Python](https://img.shields.io/badge/Python-3776AB?logo=python&logoColor=fff)](#)
[![Zed](https://img.shields.io/badge/Zed-white?logo=zedindustries&logoColor=084CCF)](https://zed.dev/)
[![ACP](https://img.shields.io/badge/ACP-0.10.5-green)](https://agentclientprotocol.com/)
[![VS Code](https://img.shields.io/badge/VS%20Code-Insiders-blue)](#)
[![IntelliJ IDEA](https://img.shields.io/badge/IntelliJIDEA-000000.svg?logo=intellij-idea&logoColor=white)](#)
[![Claude](https://img.shields.io/badge/Claude-D97757?logo=claude&logoColor=fff)](#)
[![Google Gemini](https://img.shields.io/badge/Google%20Gemini-886FBF?logo=googlegemini&logoColor=fff)](#)
[![ChatGPT](https://custom-icon-badges.demolab.com/badge/ChatGPT-74aa9c?logo=openai&logoColor=white)](#)
[![Ollama](https://img.shields.io/badge/Ollama-fff?logo=ollama&logoColor=000)](#)

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

A lean, provider-agnostic agent framework with a thin core loop, typed event model, sandboxed tool execution, pluggable transports, and an extension API.

<table width="100%">
  <tr>
    <td width="33%" align="center" valign="top">
      <img src="https://raw.githubusercontent.com/fischerf/fischerf.github.io/07d6318c4b304f44e67e228588165eb6f9f2f5b3/aar/aar.gif" alt="AAR Agent — with CLI/TUI" width="100%" />
      <br/><sub><b>AAR Agent — with CLI/TUI</b></sub>
    </td>
    <td width="33%" align="center" valign="top">
      <img src="https://raw.githubusercontent.com/fischerf/fischerf.github.io/a2f9fc10189fceeeb1a55f990248210bedfb06a8/aar/aar_acp.png" alt="AAR Agent in Zed code editor" width="100%" />
      <br/><sub><b>AAR Agent in Zed code editor</b></sub>
    </td>
    <td width="33%" align="center" valign="top">
      <img src="https://github.com/fischerf/fischerf.github.io/blob/55ef639b72de95e4fb5ba4678016261428cc2ab2/aar/aar_acp_vscode.png" alt="AAR Agent in VSCode" width="100%" />
      <br/><sub><b>AAR Agent in VSCode</b></sub>
    </td>
  </tr>
</table>

## Design goals

- **Thin core loop** — the main execution path is small and readable at a glance
- **Typed event model** — every message, tool call, and result is a typed, serializable event
- **Provider-agnostic** — swap between Anthropic, OpenAI, Ollama, Gemini, or any OpenAI-compatible endpoint without changing agent code
- **Runtime provider switching** — switch between configured providers mid-session with `/model`; conversation history is preserved
- **Safe by default** — path restrictions, command deny-lists, and approval gates built in
- **Modular transports** — the same agent runs from CLI, TUI, web API, or embedded in your code
- **Persistent sessions** — every run is saved as JSONL and resumable
- **Observable** — every provider call and tool execution is timed; sessions carry a `trace_id`
- **Cost-aware** — live token and cost tracking with configurable budget limits and visual warnings
- **Cancellable** — cooperative and hard cancellation built in
- **Extensible** — pluggable extension API with three-tier auto-discovery, event hooks, custom tools, and slash-commands

### Operating modes

| Command | Use case | Description |
|---------|----------|-------------|
| `aar run "…"` | Automation / CI | One-shot task — runs to completion and exits; no interaction |
| `aar chat` | Interactive CLI | Conversational loop in the terminal with approval prompts |
| `aar tui` | Interactive TUI | Scrollable Rich interface with live token counters |
| `aar tui --fixed` | Interactive TUI | Full-screen Textual UI with fixed header/footer bars, mouse support |
| `aar serve` | Remote / web | HTTP/SSE web API — use from a browser, curl, or remote agents |
| `aar acp` | IDE integration | [ACP](https://agentclientprotocol.com/) stdio agent for Zed and other ACP-compatible editors |
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

Set `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, or point `base_url` at a local Ollama instance.

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

# Lift the file-tool cwd restriction and load config from a JSON file
> aar chat --no-require-approval --no-restrict-to-cwd --config aar.json

# One-shot task
> aar run "Refactor main.py to use async/await"

# Skip approval prompts for scripted / CI use
> aar run --no-require-approval "Refactor main.py to use async/await"

# Start the HTTP/SSE web server
> aar serve --host 0.0.0.0 --port 8080

# Switch providers mid-session with /model (in chat, tui, or tui --fixed)
> /model gpt4
> /model ollama/llama3
```

## ACP — IDE integration

`aar acp` starts an [Agent Client Protocol](https://agentclientprotocol.com/) agent that editors like [Zed](https://zed.dev) connect to over stdio.

```bash
aar acp              # stdio — for Zed and other ACP-compatible editors
aar acp --http       # HTTP/SSE — for remote or programmatic ACP clients
```

### Zed - local dev

- add to (Linux) `~/.config/zed/settings.json`:
- add to (Windows) `%appdata%\zed\settings.json`:

```json
{
  "agent_servers": {
    "Aar Agent": {
      "type": "custom",
      "command": "aar",
      "args": ["acp"],
      "env": {}
    }
  }
}
```

### VSCode

- Requirements: Install ACP Client for VSCode: [ACP Client](https://marketplace.visualstudio.com/items?itemName=formulahendry.acp-client)

- add to (Linux) `~/.config/Code/User/settings.json`:
- add to (Windows) `%appdata%\Code\User\settings.json`:

```json
    "acp.agents": {
        "Aar Agent": {
       	"command": "aar",
       	"args": [
        		"acp",
        		"--log-level",
        		"DEBUG",
        		"--log-file",
        		"aar.log"
       	],
       	"env": {}
        }
    }
```

See [`docs/acp.md`](docs/acp.md) for the full setup guide, HTTP endpoint reference, and programmatic embedding.

## Extensions

Aar has a pluggable extension system. Extensions are Python modules that expose a `register(api)` entry point and can hook into agent lifecycle events, register custom tools, add slash-commands, and append to the system prompt.

```bash
# Install an extension from PyPI
aar install aar-ext-permission-gate

# List discovered extensions
aar extensions list

# Inspect what an extension registers
aar extensions inspect permission_gate
```

Extensions are auto-discovered from three tiers (later tiers shadow earlier ones by name):

| Priority | Location | Scope |
|----------|----------|-------|
| 1 | `aar_extensions` entry-point group | Global (pip-installed) |
| 2 | `~/.aar/extensions/` | Per-user |
| 3 | `.agent/extensions/` | Per-project |

### First-party extensions

A curated registry of extensions is maintained at [**aar-extensions-registry**](https://github.com/fischerf/aar-extensions-registry):

| Package | Description |
|---------|-------------|
| `aar-ext-permission-gate` | Block dangerous bash commands (rm -rf, sudo, mkfs, etc.) |
| `aar-ext-protected-paths` | Block writes to .env, secrets, credentials, SSH keys |
| `aar-ext-git-checkpoint` | Auto-commit at turn boundaries + rollback tool |
| `aar-ext-mcp-tools` | MCP server tool discovery via the extension API |
| `aar-ext-observability` | Structured metrics and logging per turn |

See [`docs/extensions.md`](docs/extensions.md) for the full developer guide on creating extensions.

## Architecture

```
agent/
├── core/           # Loop, agent, events, session, config
├── providers/      # LLM API adapters (Anthropic, OpenAI, Ollama, Gemini, Generic)
├── tools/          # Tool registry, schema, execution engine
├── safety/         # Policy engine, permission manager, sandboxes
├── memory/         # Session persistence (JSONL)
├── extensions/     # Extension API, loader, manager, MCP bridge, observability
│   └── contrib/    # Built-in example extensions (companion)
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

The `bash` built-in tool requires **WSL** (Windows Subsystem for Linux). Install it once:

```bash
wsl --install
```

> Not required if you do not enable the `bash` built-in tool.

For strong process isolation, use the built-in `wsl` sandbox mode — it routes all agent shell
commands through a dedicated, disposable Alpine distro instead of your main WSL environment:

```bash
aar init            # creates ~/.aar/distros/ with built-in Alpine profiles
aar sandbox setup   # one-time setup (reads profile + packages from ~/.aar/config.json)
aar sandbox status  # verify
```

Point `safety.sandbox.wsl.profile` in `~/.aar/config.json` at one of the profiles in `~/.aar/distros/`
to pre-configure the rootfs URL, packages, repo setup commands, and the system-prompt hint the model sees.
Switch distros by changing the `profile` path and running `aar sandbox reset`.

See [Safety — `wsl` sandbox mode](docs/safety.md#wsl--dedicated-wsl2-distro) for full details.

## Documentation

| Document | Contents |
|----------|----------|
| [Configuration](docs/configuration.md) | `AgentConfig` reference, config precedence, approval modes, logging, system prompt, shell, project rules |
| [Tokens & Cost](docs/tokens.md) | Token tracking pipeline, budget enforcement, cost estimation, pricing tables, TUI display |
| [ACP](docs/acp.md) | ACP stdio setup for Zed and other editors, HTTP/SSE mode, programmatic embedding, endpoint reference |
| [Providers](docs/providers.md) | Anthropic, OpenAI, Ollama, Gemini, Generic setup and options |
| [Gemini provider](docs/providers_gemini.md) | Gemini SDK mode, HTTP mode, thinking/reasoning, `extra` key reference |
| [Safety](docs/safety.md) | Deny lists, path restrictions, sandbox modes, approval callbacks |
| [MCP](docs/mcp.md) | MCP host integration — CLI config, programmatic API, transports, reference tables |
| [Web API](docs/web-api.md) | HTTP endpoints, SSE streaming, ASGI embedding, per-request safety |
| [Themes & Layout](docs/themes.md) | Built-in themes, custom themes, layout sections, full-screen fixed-bar mode, keyboard shortcut reference |
| [Extensions](docs/extensions.md) | Extension API, creating extensions, event hooks, tools, commands, auto-discovery, publishing to PyPI |
| [Development](docs/development.md) | Programmatic usage, image input, custom tools, events, sessions, cancellation, observability, testing |
| [Architecture](docs/architecture.md) | Component walkthrough, core loop, event flow, provider internals |
| [Agent Loop & Guardrails](docs/agent_loop.md) | Core loop flow diagram, guardrail mechanics, state transitions, config tuning |
| [Prompting](docs/prompting.md) | System prompt design, provider-specific tips, tool guidance |

---

## License

Apache License 2.0
