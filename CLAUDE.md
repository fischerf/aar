# CLAUDE.md — Aar Agent Framework

## What is this?
Aar is a lean, provider-agnostic AI agent framework in Python. Thin core loop (~80 lines), pluggable providers, sandboxed tool execution, typed event model.

## Quick reference

```bash
# install (dev, all providers)
pip install -e ".[all,dev]"

# tests (no API keys needed for unit tests)
pytest tests/ -v

# live provider tests (need running Ollama / API keys)
pytest tests/ -m live --live -v

# lint
ruff check agent/ tests/
ruff format agent/ tests/

# run
aar init                  # first-time setup: config.json + ~/.aar/distros/ profiles + theme templates
aar chat                  # interactive
aar run "do something"    # one-shot
aar tui                   # rich TUI (inline)
aar tui --fixed           # full-screen TUI (Textual)
aar serve                 # web API (Aar REST/SSE)
aar acp                   # ACP stdio agent (Zed, editors — uses agent-client-protocol SDK)
aar acp --http            # ACP HTTP/SSE server (REST clients)

# WSL2 sandbox (Windows — set safety.sandbox.wsl.profile in config first)
aar sandbox setup         # one-time: downloads rootfs, runs pre_install_commands, installs packages
aar sandbox reset --yes   # wipe + recreate (re-reads profile from config)
aar sandbox status        # show config + live distro state

# Inspect system prompt
aar prompt                # show assembled system prompt
aar prompt --layers       # show ordered sources (file paths, char counts, skipped files)
```

## Project layout
- `agent/core/` — loop, config, events, session, state, tokens, multimodal, logging, guardrails (the heart)
- `agent/providers/` — Anthropic, OpenAI, Ollama, generic (pluggable)
- `agent/tools/` — registry, executor, built-in tools (filesystem, shell)
- `agent/safety/` — policy engine, permissions, sandbox
- `agent/memory/` — JSONL session persistence
- `agent/extensions/` — MCP bridge, observability
- `agent/transports/` — CLI (Typer), TUI (Rich), TUI Fixed (Textual), web (ASGI), ACP, stream
  - `acp/` — package with two transports: `AarAcpAgent` (SDK stdio, for Zed — `stdio.py`) + `create_acp_asgi_app()` (HTTP REST — `http.py`); shared helpers in `common.py`
  - `tui_utils/` — shared formatting helpers for both TUI modes
  - `keybinds.py` — keyboard shortcut definitions for the fixed TUI
  - `tui_widgets/` — Textual widget classes (bars, blocks, chat body, input, log viewer, thinking panel, extension panel + confirm modal)
  - `themes/` — theme models, built-in themes, theme registry
- `tests/` — pytest + pytest-asyncio, ~870 tests; ACP coverage split across `test_acp.py` (transport units) and `test_acp_wire.py` (wire-level JSON-RPC roundtrips)

## ACP (Agent Client Protocol)

`aar acp` starts an ACP compliant server (`uvicorn` required).

```
GET  /agents                  — list agents (manifest)
GET  /agents/{name}           — single agent manifest
POST /runs                    — create run; body: {agent_name, input, mode, session_id?}
GET  /runs/{run_id}           — run status & output
POST /runs/{run_id}/cancel    — cancel an in-progress run
GET  /runs/{run_id}/events    — full ACP event log for a run
GET  /sessions/{session_id}   — session metadata
GET  /sessions/{id}/panels    — extension UI panels (+ /panels/{name}, POST …/actions/{action})
GET  /ping                    — health check
```

**Run modes** (`mode` field in `POST /runs`):
- `sync` — block until complete, return the finished `Run` object
- `async` — return `202` immediately; poll `GET /runs/{id}`
- `stream` — Server-Sent Events; each line is `data: <json>\n\n`

**SSE event types** (stream mode): `run_in_progress`, `message_created`, `run_completed`, `run_failed`, `run_cancelled`, `panel_changed`

**Run statuses**: `created` → `in-progress` → `completed` / `failed` / `cancelled`

**Programmatic use** — embed the ACP app in any ASGI framework:
```python
from agent.transports.acp import create_acp_asgi_app
app = create_acp_asgi_app(config=my_config, agent_name="aar")
```

**Stdio transport extras** (Zed / editor integration, handled by `AarAcpAgent`):
- Advertises `load_session` + `fork_session` capabilities on `initialize`
- Returns per-session `modes` (`auto` / `review` / `read-only`) and `config_options`
  (`auto_approve_writes`, `auto_approve_execute`, `read_only`) from `session/new` + `session/load`
- Implements `authenticate`, `session/fork`, `session/resume`, `session/set_mode`,
  `session/set_config_option`
- Registers an `acp_terminal` built-in tool (routes shell commands through the editor's terminal
  pane) **only** when the client advertises `ClientCapabilities.terminal = true`
- Full-spec MCP server bridge: stdio + HTTP MCP servers passed in `session/new` are started and
  their tools registered for the lifetime of that session
- Extension UI panels over custom methods `_aar/panel_list`, `_aar/panel_snapshot`,
  `_aar/panel_action` (+ `_aar/panel_changed` notification) — `ext_method` in `stdio.py`
- See `docs/acp.md` for the full event/notification matrix

## Extension UI panels

`agent/extensions/api.py` defines a transport-agnostic panel contract: `UINode` (tree),
`UIAction` (id/label/key/kinds/handler, `destructive`, `inputs`, `mutates`), `UIPanel`
(`snapshot`, `status`, `changed` event). Extensions call `api.register_panel(...)`;
`ExtensionManager.panels` merges them. No Textual in that layer — `run_ui_snapshot` /
`run_ui_action` run sync handlers in a thread. The fixed TUI mounts one `ExtensionPanel`
per registered panel in `#right-col` (toggle `ctrl+b`, action keys only while focused,
destructive → `ConfirmModal`); `_sync_right_col` collapses the column only when every
child is hidden. Tests: `tests/test_extension_panels.py`, `tests/test_acp_panels.py`.

## Zed Editor extension

`extension.toml` at the repo root registers Aar as a Zed agent-server extension.

**`aar acp` uses the official `agent-client-protocol` Python SDK** (`pip install agent-client-protocol`) for stdio transport. Zed communicates with the process over stdin/stdout — no HTTP server or port needed.

**Local dev — no archive needed.** Add this to `~/.config/zed/settings.json`:
```json
{
  "agent_servers": {
    "Aar (local)": {
      "type": "custom",
      "command": "aar",
      "args": ["acp"],
      "env": {}
    }
  }
}
```
This uses the `aar` binary already on your `$PATH` and skips any archive download.

**Published extension (release) setup:**
```bash
bash scripts/zed/build_archives.sh          # builds dist/zed/*.tar.gz + .zip
# attach the files in dist/zed/ to the GitHub Release for vX.Y.Z
# paste the printed sha256 values into the matching targets in extension.toml
```

**Important:** all `cmd` values in `extension.toml` targets must start with `./` (Zed
rejects bare filenames like `launch.cmd`).

## Rules

- **Python 3.12.2+** (`requires-python` in `pyproject.toml`). Type hints on all public functions.
- **Format/lint with `ruff`** (line length 100, target py311).
- Prefer `pathlib.Path` over `os.path`.
- Keep `agent/core/loop.py` thin — don't add logic there without good reason.
- All events must be Pydantic models — no raw dicts in the event stream.
- Never import a specific provider outside `agent/providers/`.
- Tool implementations must not bypass `agent/safety/`.
- Tests: pytest + pytest-asyncio. New features need at least one happy-path test.
- Commits: imperative mood, lowercase, no period. One concern per PR.
