# Web API

```bash
pip install uvicorn
aar serve --port 8080
```

## Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Health check |
| `/chat` | POST | Run a prompt, return full response |
| `/chat/stream` | POST | Run a prompt, stream events via SSE |
| `/sessions` | GET | List session IDs |
| `/sessions/{id}` | GET | Session details |

## `aar serve` flags

`aar serve` shares the same config-loading logic as `aar chat`/`aar run` but exposes a smaller set of flags:

| Flag | `aar chat` / `aar run` / `aar tui` | `aar serve` |
|------|:----------------------------------:|:-----------:|
| `--model`, `--provider`, `--api-key`, `--base-url` | yes | yes |
| `--config <file>` | yes | yes |
| `--read-only / --no-read-only` | yes | yes |
| `--host`, `--port` | — | yes |
| `--require-approval / --no-require-approval` | yes | — |
| `--restrict-to-cwd / --no-restrict-to-cwd` | yes | — |
| `--denied-paths`, `--allowed-paths` | yes | — |
| `--log-level` | yes | — |
| `--max-steps` | yes | — |
| `--session` | yes | — |
| `--mcp-config` | yes | — (see [MCP tools and the web server](mcp.md#mcp-tools-and-the-web-server)) |

Config not expressible via `aar serve` flags can be set in `~/.aar/config.json` — the server auto-loads it on startup.

## Approval in the web transport

There is no terminal to prompt in a server process, so the web transport **auto-approves** all tool calls by default. The HTTP request itself is treated as implicit approval. This means `require_approval_for_writes` / `require_approval_for_execute` in `SafetyConfig` have no blocking effect — use `read_only` or path restrictions instead if you need hard limits.

```bash
# Harden the server: block all writes
aar serve --read-only

# Or restrict to a specific directory tree via config file
# ~/.aar/config.json
# { "safety": { "allowed_paths": ["/my/project/**"] } }
```

## Per-request safety override

Clients can tighten or loosen safety settings for a single request by including a `"safety"` key in the JSON body. Only the fields you specify are overridden; everything else uses the server's config.

```bash
# Force read-only for this one request
curl -X POST http://localhost:8080/chat \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Summarise README.md", "safety": {"read_only": true}}'

# Allow writes but restrict to a specific path
curl -X POST http://localhost:8080/chat \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Write hello.py", "safety": {"allowed_paths": ["/tmp/**"]}}'
```

## `/chat` — request and response

```bash
curl -X POST http://localhost:8080/chat \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Write hello.py", "session_id": null}'
```

Response JSON shape:

```json
{
  "session_id": "a3f1b2c4d5e6",
  "state":      "completed",
  "step_count": 2,
  "response":   "Here is the file I wrote.",
  "tool_results": [
    {
      "tool_name":   "write_file",
      "output":      "Written 42 bytes to hello.py",
      "is_error":    false,
      "duration_ms": 3.1
    }
  ],
  "events": [ ... ]
}
```

| Field | Description |
|-------|-------------|
| `response` | Final assistant text. If the model completed via tools without producing any narrating text, this falls back to the last successful tool output so you always get something meaningful. |
| `tool_results` | Ordered list of every tool call result in the run. Empty when no tools were used. |
| `state` | `"completed"` \| `"error"` \| `"cancelled"` — use this to detect failures cleanly. |
| `events` | Full ordered event log: `user_message`, `tool_call`, `tool_result`, `assistant_message`, `provider_meta`, `session` (ended), etc. Inspect these when you need the fine-grained trace. |

## `/chat/stream` — SSE event stream

```bash
curl -N http://localhost:8080/chat/stream \
  -X POST -H "Content-Type: application/json" \
  -d '{"prompt": "Write hello.py"}'
```

Events arrive as standard SSE frames, one per agent event:

```
event: tool_call
data: {"type":"tool_call","tool_name":"write_file","arguments":{"path":"hello.py","content":"..."},...}

event: tool_result
data: {"type":"tool_result","tool_name":"write_file","output":"Written 42 bytes","is_error":false,...}

event: assistant_message
data: {"type":"assistant_message","content":"Done — hello.py has been created.","stop_reason":"end_turn",...}

event: session
data: {"type":"session","data":{"state":"completed","step_count":2},"action":"ended",...}
```

**The `session` event with `action: "ended"` is the definitive done signal.** It is always emitted as the last event before the stream closes, and carries `data.state` (`"completed"` / `"error"` / `"cancelled"`) and `data.step_count`. Do not rely solely on stream-close to detect completion — the ended event lets you distinguish a clean finish from a network drop.

Summary of all event types you may receive:

| SSE `event:` field | When emitted | Key fields |
|--------------------|--------------|------------|
| `provider_meta` | After each LLM call | `usage`, `duration_ms`, `model` |
| `reasoning` | Extended-thinking models only | `content` |
| `tool_call` | Before each tool executes | `tool_name`, `arguments` |
| `tool_result` | After each tool finishes | `tool_name`, `output`, `is_error`, `duration_ms` |
| `assistant_message` | Each LLM text turn | `content`, `stop_reason` (`end_turn` \| `tool_use`) |
| `error` | Provider or safety failure | `message`, `recoverable` |
| `session` | Stream start and **stream end** | `action` (`"started"` \| `"ended"`), `data.state` |

## Embed the ASGI app

```python
from agent.transports.web import create_asgi_app
from agent.core.config import load_config
from pathlib import Path
import uvicorn

# Explicit config (or omit to auto-load ~/.aar/config.json)
config = load_config(Path("myconfig.json"))

app = create_asgi_app(config)
uvicorn.run(app, host="0.0.0.0", port=8080)
```

`create_asgi_app` accepts three optional arguments:

| Argument | Default | Description |
|----------|---------|-------------|
| `config` | `None` | `AgentConfig`. If `None`, auto-loads `~/.aar/config.json` or uses built-in defaults. |
| `approval_callback` | `_auto_approve_callback` | Async callable `(ToolSpec, ToolCall) -> ApprovalResult`. Override for webhook-style approval. |
| `registry` | `None` | Shared `ToolRegistry`. Used to expose MCP tools across all requests (see [MCP docs](mcp.md#mcp-tools-and-the-web-server)). |
