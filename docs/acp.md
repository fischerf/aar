# ACP — Agent Communication Protocol

Aar implements [ACP v0.2](https://agentcommunicationprotocol.dev) in two modes:

| Mode | Command | Transport | Use case |
|------|---------|-----------|----------|
| **stdio** | `aar acp` | stdin / stdout | Zed and any ACP-compatible editor |
| **HTTP/SSE** | `aar acp --http` | HTTP REST + SSE | Remote or programmatic ACP clients |

### Advertised capabilities

On connection, Aar reports the following capabilities to the editor:

| Capability | Value | Meaning |
|------------|-------|---------|
| `load_session` | `true` | Editor can resume previously saved sessions |
| `session.list` | supported | Editor can show Aar session history in its sidebar |
| `session.close` | supported | Editor notifies Aar when a session tab is closed |
| `prompt.embedded_context` | `true` | `@`-mentions embed file contents that Aar reads |

---

## 1. Zed Editor — stdio setup

Zed communicates with the agent over stdin/stdout using the
`agent-client-protocol` SDK. No HTTP server or port is needed.

### Local development

Add to `~/.config/zed/settings.json`:

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

Make sure `aar` is on your `$PATH` (`pip install -e ".[all,acp]"` from the repo root).

### Published extension (release)

`extension.toml` at the repo root registers Aar as a Zed extension. Platform
archives (`.tar.gz` / `.zip`) are attached to GitHub Releases and contain a
launcher script that installs `aar-agent` from PyPI on first run.

Build the archives after bumping the version:

```bash
bash scripts/zed/build_archives.sh
# Attach the files in dist/zed/ to the GitHub Release for vX.Y.Z.
# Update the sha256 values in extension.toml.
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AAR_LAUNCHED_BY` | — | Set to `"zed"` by the extension; useful for log filtering |
| `ANTHROPIC_API_KEY` | — | Required for the Anthropic provider |
| `OPENAI_API_KEY` | — | Required for the OpenAI provider |

Set these in Zed's **Settings → Agent Servers → Environment** or in your shell profile.

### Logging

```bash
# Write logs to a file — useful for diagnosing Zed connection issues.
aar acp --log-level debug --log-file /tmp/aar-acp.log
```

Logs always go to stderr (or the log file) and never to stdout, so they cannot
corrupt the JSON-RPC stream that Zed reads.

---

## 2. Other ACP-compatible editors

Any editor that supports ACP stdio agents works the same way — point it at
`aar acp`. Check your editor's documentation for the equivalent of Zed's
`"type": "custom"` agent server config.

---

## 3. HTTP/SSE mode

```bash
aar acp --http                       # 127.0.0.1:8000
aar acp --http --host 0.0.0.0 --port 9000
```

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/ping` | Health check — returns `{"status": "ok"}` |
| `GET` | `/agents` | List agents (manifest array) |
| `GET` | `/agents/{name}` | Single agent manifest |
| `POST` | `/runs` | Create a run |
| `GET` | `/runs/{run_id}` | Run status and output |
| `POST` | `/runs/{run_id}/cancel` | Cancel an in-progress run |
| `GET` | `/runs/{run_id}/events` | Full ACP event log for a run |
| `GET` | `/sessions/{session_id}` | Session metadata |

### Run modes

Set the `mode` field in `POST /runs`:

| Mode | Behaviour |
|------|-----------|
| `sync` | Blocks until the run completes; returns the finished `Run` object |
| `async` | Returns `202` immediately; poll `GET /runs/{id}` for status |
| `stream` | Server-Sent Events; each line is `data: <json>\n\n` |

### Run lifecycle

```
created → in-progress → completed
                      → failed
                      → cancelled
```

### SSE event types (stream mode)

| Event type | Description |
|------------|-------------|
| `run_in_progress` | Run has started |
| `message_created` | A new assistant message chunk is available |
| `run_completed` | Run finished successfully |
| `run_failed` | Run terminated with an error |
| `run_cancelled` | Run was cancelled |

### Quick example

```bash
# Create a sync run
curl -s -X POST http://127.0.0.1:8000/runs \
  -H "Content-Type: application/json" \
  -d '{
    "agent_name": "aar",
    "mode": "sync",
    "input": [{"role": "user", "parts": [{"content_type": "text/plain", "content": "Hello!"}]}]
  }' | python -m json.tool

# Stream a run
curl -s -N -X POST http://127.0.0.1:8000/runs \
  -H "Content-Type: application/json" \
  -d '{"agent_name": "aar", "mode": "stream", "input": [{"role": "user", "parts": [{"content": "Hello!"}]}]}'
```

---

## 4. Session lifecycle

### What Aar handles on each ACP event

| ACP event | What Aar does |
|-----------|---------------|
| `initialize` | Returns capabilities, protocol version, and agent info |
| `session/new` | Creates a new Aar session; stores `cwd` in session metadata; starts any MCP servers passed by the editor |
| `session/load` | Resumes a saved session from `~/.aar/sessions/`; returns `null` if not found (editor creates new) |
| `session/list` | Returns all saved sessions with title (first assistant message) and `cwd` |
| `session/close` | Cleans up in-memory session state and shuts down any per-session MCP bridges |
| `session/prompt` | Runs the Aar agent loop; streams thinking, tool calls, and token updates back to the editor |
| `session/cancel` | Sets the agent's cooperative cancel signal for the current prompt |

### Streaming updates pushed during a prompt

| Update type | Trigger |
|-------------|---------|
| `agent_message_chunk` | Each streaming token (when provider streaming is enabled) or complete assistant message |
| `agent_thought_chunk` | Extended thinking / reasoning content |
| `tool_call` (start) | When the agent calls a tool |
| `tool_call_update` (progress) | When the tool returns its result (status: `completed` or `failed`) |
| `plan` | Updated after each tool call and result — shows the current step list with statuses |
| `usage_update` | After each provider call — reports token count and estimated cost |
| `session_info_update` | After the first assistant response — sets the session title in the editor sidebar |
| `available_commands_update` | Once per session on first prompt — advertises `/model` and `/clear` slash commands |

### Permission requests

By default, Aar forwards tool-approval prompts to the editor via the ACP `request_permission` mechanism.
The editor shows an **Allow / Deny** dialog before each tool executes.
If no editor connection is available, tools are auto-approved.

### MCP servers from the editor

When an editor passes MCP server configurations in `session/new` or `session/load`, Aar starts those
MCP bridges and registers their tools for the lifetime of that session.
Both HTTP and stdio MCP transports are supported:

```json
// Passed by editor in session/new mcp_servers
{"command": "npx", "args": ["@modelcontextprotocol/server-filesystem", "/my/project"], "name": "fs"}
{"url": "http://localhost:3000/mcp", "name": "remote-mcp"}
```

The bridges are shut down automatically when `session/close` is received.

### Model selection

Aar advertises a `/model` slash command via `AvailableCommandsUpdate` so editors can offer model switching.
When the editor calls `set_session_model`, Aar maps the model ID to a provider and applies it for the
remainder of that session without affecting other sessions:

| Model ID prefix | Provider |
|-----------------|----------|
| `claude-*` | Anthropic |
| `gpt-*`, `o1-*`, `o3-*`, `o4-*`, `chatgpt-*` | OpenAI |
| anything else | Ollama |

In Zed, model switching is available once Zed exposes a UI for `set_session_model`.
Until then you can watch the debug log (`--log-level debug`) to confirm the call arrives and is applied.

### Plan tracking

Every tool call during a prompt builds a live plan that Aar streams back as `plan` updates:

- When a tool starts → a new `PlanEntry` with status `in_progress` is appended.
- When the tool returns → the entry flips to `completed` (or stays `in_progress` on error).

Editors that render the plan panel (e.g. Zed's tool-call sidebar) show each step as it executes.
No configuration is needed — the plan is always active.

---

## 5. Programmatic embedding

Embed the ACP HTTP app inside any ASGI framework (FastAPI, Starlette, etc.):

```python
from agent.transports.acp import create_acp_asgi_app

app = create_acp_asgi_app(
    config=my_config,        # AgentConfig — optional, loads ~/.aar/config.json by default
    agent_name="aar",
    agent_description="My embedded Aar agent",
)

# Mount under uvicorn, FastAPI, or any ASGI server.
```

For stdio use:

```python
import asyncio
from agent.transports.acp import run_acp_stdio

asyncio.run(run_acp_stdio(config=my_config, agent_name="aar"))
```

---

## 6. Dependencies

ACP support requires the `agent-client-protocol` package:

```bash
pip install "aar-agent[acp]"
# or
pip install agent-client-protocol
```

The package is listed as an optional dependency so the rest of Aar works without
it. Only `aar acp` (stdio) and `create_acp_asgi_app` need it at runtime.
