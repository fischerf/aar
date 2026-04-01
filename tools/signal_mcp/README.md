# Signal MCP Server

A lightweight **Model Context Protocol (MCP) server** that exposes Signal messaging
as a set of tools any MCP-compatible client (Claude Desktop, Claude Code, etc.) can call.

Built on top of the community [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api)
project — no proprietary binaries required, no cloud relay.

---

## Architecture

```
┌────────────────────────────────────────────────────────────┐
│  MCP Client (Claude Desktop / Claude Code / custom app)    │
└───────────────────────────┬────────────────────────────────┘
                            │ stdio (JSON-RPC)
┌───────────────────────────▼────────────────────────────────┐
│                  signal-mcp  (this server)                 │
│                                                            │
│  server.py ──► signal_client.py ──► signal-cli REST API    │
│  auth.py   (onboarding wizard)      http://localhost:8080  │
│  config.py (persistent config)                             │
└───────────────────────────┬────────────────────────────────┘
                            │ HTTP / REST
┌───────────────────────────▼────────────────────────────────┐
│              signal-cli-rest-api (Docker)                  │
│                                                            │
│  Manages the Signal protocol, keys, and sockets.           │
└────────────────────────────────────────────────────────────┘
```

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python ≥ 3.11 | `match` statement required |
| Docker (recommended) | For running signal-cli-rest-api |
| A phone number | Real SIM or VoIP (e.g. Google Voice) |

---

## Quick Start

### 1 — Start signal-cli-rest-api

```bash
docker run -d \
  --name signal-api \
  -p 8080:8080 \
  -e MODE=normal \
  -v $HOME/.local/share/signal-api:/home/.local/share/signal-cli \
  bbernhard/signal-cli-rest-api:latest
```

> **Tip:** The REST API Swagger UI is available at `http://localhost:8080/v1/docs`.

### 2 — Install signal-mcp

```bash
git clone <repo>
cd signal-mcp

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate

pip install -r requirements.txt
# or: pip install -e .
```

### 3 — Onboarding (first-time setup)

Run the interactive wizard — it will register your number, verify via SMS,
and save a config file to `~/.signal-mcp/config.json`.

```bash
python -m signal_mcp onboard
```

Follow the prompts:

```
Step 1/3 — API Connection
  signal-cli REST API URL [http://localhost:8080]:
  ✓ Connected — 0 account(s) registered

Step 2/3 — Phone Number
  Phone number (+15551234567): +15559876543

Step 3/3 — Registration
  [1] Register as new primary device (SMS / voice)
  [2] Link as secondary device (QR code)
  [3] Skip (already registered)
  Choice: 1

  Requesting verification code … ✓ Code sent.
  Enter verification code: 123456
  ✓ Verified +15559876543
  ✓ Configuration saved
```

### 4 — Test from the command line

```bash
# Send a message
python -m signal_mcp send +15551234567 "Hello from Signal MCP!"

# Receive pending messages
python -m signal_mcp receive
```

### 5 — Connect to Claude Desktop

Merge the following into your Claude Desktop config
(`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "signal": {
      "command": "python",
      "args": ["-m", "signal_mcp"],
      "cwd": "/absolute/path/to/signal-mcp",
      "env": {
        "SIGNAL_API_URL": "http://localhost:8080",
        "SIGNAL_PHONE_NUMBER": "+15559876543",
        "SIGNAL_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

---

## Available MCP Tools

| Tool | Description |
|---|---|
| `signal_send_message` | Send a message to one or more numbers / groups |
| `signal_react` | React to a message with an emoji |
| `signal_receive_messages` | Poll for new incoming messages |
| `signal_list_groups` | List all groups |
| `signal_create_group` | Create a new group |
| `signal_list_contacts` | Fetch address book |
| `signal_update_contact` | Set display name / expiry timer |
| `signal_set_profile` | Update account profile |
| `signal_account_info` | Get account registration info |
| `signal_list_accounts` | List all accounts on the REST API |

---

## Configuration Reference

All values can be set via environment variables, `.env` file, or `~/.signal-mcp/config.json`.

| Variable | Default | Description |
|---|---|---|
| `SIGNAL_API_URL` | `http://localhost:8080` | signal-cli REST API base URL |
| `SIGNAL_PHONE_NUMBER` | — | Your Signal number (E.164) |
| `SIGNAL_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `SIGNAL_LOG_FILE` | — | Optional log file path |
| `SIGNAL_MCP_CONFIG` | `~/.signal-mcp/config.json` | Config file location |

---

## Project Structure

```
signal-mcp/
├── signal_mcp/
│   ├── __init__.py          # Package exports
│   ├── __main__.py          # CLI entry-point (serve / onboard / send / receive)
│   ├── server.py            # MCP Server — tool definitions & dispatcher
│   ├── signal_client.py     # Async HTTP client for signal-cli REST API
│   ├── auth.py              # Interactive onboarding wizard (Rich TUI)
│   ├── config.py            # Pydantic config model + load/save helpers
│   └── logging_config.py    # Rich + stdlib logging setup
├── requirements.txt
├── pyproject.toml
├── .env.example
├── claude_desktop_config.example.json
└── README.md
```

---

## Linking as a Secondary Device (no SMS required)

If Signal is already installed on your phone and you don't want to use SMS:

```
python -m signal_mcp onboard
# Choose option [2] at Step 3
```

A device-link URI is generated. Encode it as a QR code:

```bash
qrencode -t UTF8 'tsdevice:/?uuid=…'
# or paste into https://qr.io
```

Then scan it in **Signal → Settings → Linked Devices → Link New Device**.

---

## Timeout & Error Handling

* Every tool call is wrapped in `asyncio.wait_for` with a configurable timeout.
* Network errors surface as `SignalError` and are returned to the MCP client as `isError=True`.
* The receive poller uses a double-layer timeout: inner REST timeout + outer `asyncio.wait_for` safety margin.
* All errors are logged with full Rich tracebacks (at DEBUG level).

---

## Security Notes

* Runs **entirely on-premise** — no data leaves your machine.
* The signal-cli-rest-api has **no built-in authentication** by default. Bind it to `127.0.0.1:8080` (already the default above) or add reverse-proxy auth in production.
* Keep your `.env` / config file with appropriate file permissions (`chmod 600`).

---

## License

Apache License 2.0 
