# ACP — Agent Communication Protocol

Aar implements [ACP v0.2](https://agentcommunicationprotocol.dev) in two modes:

| Mode | Command | Transport | Use case |
|------|---------|-----------|----------|
| **stdio** | `aar acp` | stdin / stdout | Zed and any ACP-compatible editor |
| **HTTP/SSE** | `aar acp --http` | HTTP REST + SSE | Remote or programmatic ACP clients |

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

## 4. Programmatic embedding

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

## 5. Dependencies

ACP support requires the `agent-client-protocol` package:

```bash
pip install "aar-agent[acp]"
# or
pip install agent-client-protocol
```

The package is listed as an optional dependency so the rest of Aar works without
it. Only `aar acp` (stdio) and `create_acp_asgi_app` need it at runtime.
