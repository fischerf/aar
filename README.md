# Aar — Adaptive Action & Reasoning Agent

[![Website](https://img.shields.io/badge/website-fischerf.github.io%2Faar-blue?style=flat-square)](https://fischerf.github.io/aar/)

A lean, provider-agnostic agent framework with a thin core loop, typed event model, sandboxed tool execution, and pluggable transports.

## Design goals

- **Thin core loop** — the main execution path is small and readable at a glance
- **Typed event model** — every message, tool call, and result is a typed, serializable event
- **Provider-agnostic** — swap between Anthropic, OpenAI, Ollama, or any OpenAI-compatible endpoint without changing agent code
- **Safe by default** — path restrictions, command deny-lists, and approval gates built in
- **Modular transports** — the same agent runs from CLI, TUI, web API, or embedded in your code
- **Persistent sessions** — every run is saved as JSONL and resumable
- **Observable** — every provider call and tool execution is timed; sessions carry a `trace_id`
- **Cancellable** — cooperative and hard cancellation built in

## Installation

```bash
# Everything at once
pip install "aar-agent[all,dev]"

# Provider-specific
pip install "aar-agent[anthropic]"
pip install "aar-agent[openai]"
pip install "aar-agent[ollama]"
pip install "aar-agent[generic]"

# With MCP support
pip install "aar-agent[ollama,mcp]"

# Core only (no LLM provider)
pip install aar-agent
```

> **Note:** `aar-agent` is not published to PyPI.
> Use the **from-source install** below.

### Installing from source

```bash
git clone https://github.com/fischerf/aar.git
cd aar

# Full dev setup
pip install -e ".[all,dev]"

# Verify
aar --help
pytest tests/ -v
```

The `-e` flag creates a live link — editing files under `agent/` is reflected instantly without reinstalling.

## Quick start

Set `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or point `base_url` at a local Ollama instance.

## CLI

```bash
# Interactive chat (asks before write/execute, file tools restricted to cwd)
aar chat

# Chat with a specific provider/model
aar chat --provider openai --model gpt-4o
aar chat --provider ollama --model llama3

# Disable the workspace sandbox for full access
aar chat --no-require-approval --no-restrict-to-cwd

# One-shot task
aar run "Refactor main.py to use async/await"

# Skip approval prompts for scripted / CI use
aar run --no-require-approval "Refactor main.py to use async/await"

# Load full config from a JSON file
aar chat --config aar.json

# Resume a previous session
aar chat --session <session-id>

# List saved sessions
aar sessions

# List available tools
aar tools

# Launch the rich TUI
aar tui

# Start the HTTP/SSE web server
aar serve --host 0.0.0.0 --port 8080
```

### Verbose mode

Pass `--verbose` (or `-v`) to enable richer feedback: side-effect badges (`[read]`, `[write]`, `[exec]`), path highlighting, and execution timing.

```bash
aar chat --verbose
aar tui --verbose --mcp-config tools/mcp_web.json --provider ollama --model qwen3.5:9b
```

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

Neither is required if you do not enable the `bash` built-in tool.

## Documentation

| Document | Contents |
|----------|----------|
| [Configuration](docs/configuration.md) | `AgentConfig` reference, config precedence, approval modes, logging, system prompt, shell, project rules |
| [Providers](docs/providers.md) | Anthropic, OpenAI, Ollama, Generic setup and options |
| [Safety](docs/safety.md) | Deny lists, path restrictions, sandbox modes, approval callbacks |
| [MCP](docs/mcp.md) | MCP host integration — CLI config, programmatic API, transports, reference tables |
| [Web API](docs/web-api.md) | HTTP endpoints, SSE streaming, ASGI embedding, per-request safety |
| [Development](docs/development.md) | Programmatic usage, image input, custom tools, events, sessions, cancellation, observability, testing |
| [Architecture](docs/architecture.md) | Component walkthrough, core loop, event flow, provider internals |
| [Prompting](docs/prompting.md) | System prompt design, provider-specific tips, tool guidance |

---

## License

Apache License 2.0
