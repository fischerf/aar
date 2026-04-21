# ACP — Agent Communication Protocol

Aar implements [ACP v0.9.0](https://agentclientprotocol.com/) in two modes:

| Mode | Command | Transport | Use case |
|------|---------|-----------|----------|
| **stdio** | `aar acp` | stdin / stdout | Zed and any ACP-compatible editor |
| **HTTP/SSE** | `aar acp --http` | HTTP REST + SSE | Remote or programmatic ACP clients |

### Advertised capabilities

On connection, Aar reports the following capabilities to the editor:

| Capability | Value | Meaning |
|------------|-------|---------|
| `load_session` | `true` | Editor can resume previously saved sessions |
| `fork_session` | supported | Editor can branch a session at a specific message |
| `session.list` | supported | Editor can show Aar session history in its sidebar |
| `session.close` | supported | Editor notifies Aar when a session tab is closed |
| `session.set_mode` | supported | Editor can switch between `auto` / `review` / `read-only` modes |
| `session.set_config_option` | supported | Editor can toggle `auto_approve_writes` / `auto_approve_execute` / `read_only` at runtime |
| `prompt.embedded_context` | `true` | `@`-mentions embed file contents that Aar reads |
| `mcp_capabilities.http` | `true` | Editor forwards HTTP MCP servers to Aar |
| `mcp_capabilities.sse` | `false` | SSE transport not supported (servers skipped with warning) |

### Client capabilities consumed

Aar inspects the editor's advertised `ClientCapabilities` during `initialize` and gates optional
features accordingly:

| Client capability | Effect when present |
|-------------------|---------------------|
| `terminal` | Aar registers the [`acp_terminal` tool](#acp-terminal-tool) so the agent can run shell commands through the editor's terminal pane instead of a local subprocess |
| `fs.read_text_file` / `fs.write_text_file` | Aar routes file reads/writes through the editor (transparent to the agent) |

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
| `initialize` | Returns capabilities, protocol version, and agent info; stashes client capabilities for later gating |
| `authenticate` | No-op success response — Aar uses provider API keys from env/config, not per-session auth |
| `session/new` | Creates a new Aar session; stores `cwd` in session metadata; starts any MCP servers passed by the editor; returns the session's current `modes` and `config_options` |
| `session/load` | Resumes a saved session from `~/.aar/sessions/`; returns `null` if not found (editor creates new); also returns the resumed session's `modes` and `config_options` |
| `session/list` | Returns all saved sessions with title (first assistant message) and `cwd` |
| `session/close` | Cleans up in-memory session state and shuts down any per-session MCP bridges |
| `session/prompt` | Runs the Aar agent loop; streams thinking, tool calls, and token updates back to the editor |
| `session/cancel` | Sets the agent's cooperative cancel signal for the current prompt |
| `session/fork` | Branches a session at a given message index; returns a fresh `session_id` with the trimmed history |
| `session/resume` | Re-attaches to an existing session by id (equivalent to `load` for saved sessions, but does not replay history) |
| `session/set_mode` | Switches the session between `auto` / `review` / `read-only`; emits a `current_mode_update` notification |
| `session/set_config_option` | Toggles a boolean safety config at runtime (`auto_approve_writes` / `auto_approve_execute` / `read_only`); emits a `config_option_update` notification |

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

**Approval timeout** — `make_acp_approval_callback(..., timeout=<seconds>)`
caps how long Aar waits for the editor to return a decision before auto-denying
the request. `timeout=0` (the default) means wait indefinitely. The factory
validates the argument up-front and raises `ValueError` for negative, `NaN`,
`inf`, or non-numeric values (including `bool`), so misconfigurations fail
fast at wire-up time rather than silently denying every request later.

### MCP servers from the editor

When an editor passes MCP server configurations in `session/new` or `session/load`, Aar starts those
MCP bridges and registers their tools for the lifetime of that session.
Both HTTP and stdio MCP transports are supported. SSE transport is not supported — SSE servers are
skipped with a warning.

The bridges are shut down automatically when `session/close` is received.

#### Configuring MCP servers in Zed

In Zed, MCP servers are declared under `"context_servers"` in `~/.config/zed/settings.json`.
Zed forwards these to Aar automatically when it creates or loads a session.

**Schema:**

```json
{
  "context_servers": {
    "<server-name>": {
      "command": "<executable>",
      "args": ["<arg1>", "..."],
      "env": {}
    }
  }
}
```

#### Bundled MCP tools — Zed config

The `tools/` directory in the Aar repo contains ready-to-use MCP servers.
Use absolute paths because Zed does not run from the repo root.

Replace `/path/to/aar` with your actual clone path.

```json
{
  "context_servers": {
    "aar-web": {
      "command": "python",
      "args": [
        "-c",
        "import sys; sys.path.insert(0, '/path/to/aar/tools'); from web_mcp.server import mcp; mcp.run(transport='stdio')"
      ],
      "env": {}
    },
    "aar-gitlab": {
      "command": "python",
      "args": ["/path/to/aar/tools/gitlab_mcp/server.py"],
      "env": {}
    },
    "aar-signal": {
      "command": "python",
      "args": [
        "-c",
        "import sys; sys.path.insert(0, '/path/to/aar/tools'); from signal_mcp.server import mcp; mcp.run(transport='stdio')"
      ],
      "env": {}
    },
    "aar-chrome": {
      "command": "npx",
      "args": ["chrome-devtools-mcp@latest", "--no-usage-statistics"],
      "env": {}
    }
  }
}
```

> **Tip:** You can also load any of the bundled servers directly from the Aar CLI using the `--mcp`
> flag instead of wiring them through Zed:
> ```bash
> aar chat --mcp tools/mcp_web.json
> ```

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

### Session modes and config options

Each session advertises a set of **modes** and **config options** that the editor can show in a picker
and toggle at runtime. Aar derives the defaults from the current `SafetyConfig`:

| Mode | Meaning |
|------|---------|
| `auto` | No approval prompts — writes and execute run automatically |
| `review` | Approval required before writes and execute (the default for most safety configs) |
| `read-only` | Writes and execute are denied entirely; only read-side tools run |

The three config options, each a boolean toggle:

| Option | Effect |
|--------|--------|
| `auto_approve_writes` | When `true`, disables approval prompts for write-side tools (`write_file`, `edit_file`, …) |
| `auto_approve_execute` | When `true`, disables approval prompts for `bash` and other execute-side tools |
| `read_only` | When `true`, writes and execute are blocked outright |

#### How `config.json` seeds the picker

On `session/new` and `session/load`, Aar reads the loaded `AgentConfig.safety` and reports the
current mode back to the editor. The mapping from `SafetyConfig` flags to the advertised
`current_mode_id` and `current_value`s:

| `safety.read_only` | `require_approval_for_writes` | `require_approval_for_execute` | Advertised `current_mode_id` | `auto_approve_writes` | `auto_approve_execute` | `read_only` |
|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| `true`  | *any* | *any*  | `read-only` | `false` | `false` | `true`  |
| `false` | `true`  | `true`  | `review` *(default)* | `false` | `false` | `false` |
| `false` | `false` | `false` | `auto` | `true`  | `true`  | `false` |
| `false` | `true`  | `false` | `review` *(hybrid)* | `false` | `true`  | `false` |
| `false` | `false` | `true`  | `review` *(hybrid)* | `true`  | `false` | `false` |

So a config with `read_only: false` and both `require_approval_for_*: true` (the Aar default, and
what `aar init` writes) opens in **Review** with all three options unchecked.

#### What the editor writes back

When the editor calls `session/set_mode` or `session/set_config_option`, Aar applies the change
**per-session and in-memory only** — it never touches the loaded `AgentConfig` on disk or the
global `self._config`:

| Editor action | Writes |
|---------------|--------|
| `set_mode("auto")` | `require_approval_for_writes=false`, `require_approval_for_execute=false`, `read_only=false` — atomic |
| `set_mode("review")` | `require_approval_for_writes=true`, `require_approval_for_execute=true`, `read_only=false` — atomic |
| `set_mode("read-only")` | `require_approval_for_writes=true`, `require_approval_for_execute=true`, `read_only=true` — atomic |
| `set_config_option("auto_approve_writes", v)` | `require_approval_for_writes = not v` — single flag |
| `set_config_option("auto_approve_execute", v)` | `require_approval_for_execute = not v` — single flag |
| `set_config_option("read_only", v)` | `read_only = v` — single flag |

`set_mode` is coarse (rewrites all three flags together); `set_config_option` is fine-grained
(one flag at a time). After `set_mode("auto")` followed by `set_config_option("read_only", true)`
you end up in a hybrid state — `_build_mode_state` re-reads the flags on the next load and would
then report `read-only` because `read_only` wins the precedence check.

Both call paths:

1. Read the active config from `self._session_configs.get(session_id, self._config)`.
2. Produce a new immutable `SafetyConfig` via `model_copy(update=...)`.
3. Write the result back into `self._session_configs[session_id]`.
4. Emit a `current_mode_update` (for `set_mode`) or `config_option_update` (for `set_config_option`)
   notification so any other panel in the editor stays in sync.

The new config takes effect on the **next tool call** — `_make_aar_agent` reads the per-session
`SafetyConfig` every time a prompt starts, so the `PermissionManager` picks up the change
immediately.

#### Scope and persistence

| Aspect | Behaviour |
|--------|-----------|
| Config fields affected by editor toggles | Only `safety.read_only`, `safety.require_approval_for_writes`, `safety.require_approval_for_execute` |
| Fields **never** touched | `provider`, `tools.*`, `denied_paths`, `allowed_paths`, `sandbox.*`, `guardrails`, `max_steps`, `token_budget`, `cost_limit`, … |
| Storage | `AarAcpAgent._session_configs[session_id]` (in-memory dict) |
| Lifetime | Wiped on `session/close` or agent shutdown |
| Persistence | **Not** written to `~/.aar/config.json`; a fresh process re-reads the on-disk file |
| `session/fork` | Copies the parent session's override into the new session id — child starts with the same mode |
| `session/load` on a previously toggled session | Re-reads `config.json` defaults; editor-side changes from a previous process run are lost |

In short: the mode picker and toggles are a runtime UI over the narrow subset of `SafetyConfig` that
governs approvals, scoped to the current editor session. Everything else in `config.json` — your
Ollama model, sandbox profile, deny-lists, budgets — stays authoritative and untouched.

### <a name="acp-terminal-tool"></a>ACP terminal tool

When the editor advertises `ClientCapabilities.terminal = true` during `initialize`, Aar registers
an `acp_terminal` built-in tool. The LLM can call it exactly like `bash`, but instead of launching
a local subprocess Aar drives the editor's terminal pane via the ACP `terminal/*` method family:

```
terminal/create   → terminal/wait_for_exit   → terminal/output   → terminal/release
```

Benefits:

- Commands run in the editor's own PTY — users see output in the familiar terminal pane
- `cwd` and `env` can be set per invocation; output is captured and returned to the agent
- A `timeout` argument (default 60s) caps the wait; timeouts trigger `terminal/kill` + `terminal/release`
  so the editor never leaks PTYs

If the client does **not** advertise terminal support, `acp_terminal` is not registered — the agent
falls back to the local `bash` tool (which still honors Aar's sandbox and deny-list). This avoids
issuing `terminal/*` calls to a peer that would reject them.

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

---

## 7. Module layout

`agent/transports/acp/` is a package split by transport:

| File | Purpose |
|------|---------|
| `__init__.py` | Re-exports the full public API (`AarAcpAgent`, `run_acp_stdio`, `AcpTransport`, `create_acp_asgi_app`, data models) so existing imports keep working |
| `common.py` | Helpers shared by both transports: config loading, tool-kind mapping, prompt-block extraction, stop-reason mapping, MCP config translation, provider inference, session mode/config builders |
| `stdio.py` | `AarAcpAgent` + `run_acp_stdio` — SDK-backed stdio transport (Zed, editors) |
| `http.py` | `AcpTransport`, `create_acp_asgi_app`, and the Pydantic run/event models — HTTP + SSE transport |

The sibling `agent/transports/acp_permissions.py` provides `make_acp_approval_callback`,
which both transports use when wiring Aar's `PermissionManager` to ACP's
`request_permission` flow.

The `acp_terminal` built-in tool lives at `agent/tools/builtin/acp_terminal.py` and is registered
by the stdio transport at session-setup time when the client advertises terminal support.

### Concurrency model (stdio)

`AarAcpAgent` enforces **at most one in-flight prompt per session**:

- A per-session `asyncio.Lock` guards lifecycle mutations (create/close/prompt-start/prompt-end)
- A second prompt arriving while the first is still running is rejected with `RuntimeError`
- `close_session` cancels the in-flight prompt (if any) and awaits it before tearing down
- Fire-and-forget tasks (e.g. `session_update` delivery) are tracked in a strong-reference set
  so they cannot be silently garbage-collected mid-await; exceptions are logged
- `AarAcpAgent.shutdown()` drains outstanding background tasks and cancels unfinished prompts
