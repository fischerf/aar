# aar webchat

A tiny single-file HTML client for `aar serve`. No build step, no
dependencies — just open `index.html` in a browser.

## Features

- Streams `/chat/stream` (SSE) with token-by-token rendering of the
  assistant reply, or one-shot `/chat`
- Renders user / assistant / reasoning / tool-call / tool-result / error
- Collapsible **Events** side panel showing every raw SSE event
  (`stream_chunk`, `provider_meta`, `context_window`, etc.) as JSON,
  with a "Copy" button for the full transcript
- Persists server URL, session id, provider, mode, and panel state in
  `localStorage`
- Stop button aborts the in-flight request
- "Ping" button hits `/health`

## Usage

1. Start the server:

   ```bash
   aar serve --host 0.0.0.0 --port 8080
   ```

2. Open `tools/webchat/index.html` in a browser. Either:
   - Double-click the file (works because the server allows CORS `*`), or
   - Serve it locally, e.g. `python -m http.server 5500` from
     `tools/webchat/` and visit http://localhost:5500/.

3. Confirm the **Server** field points at your `aar serve` instance
   (default `http://localhost:8080`), then click **Ping** — status should
   read `health: {"status":"ok"}`.

4. Type a message and hit Enter. Shift+Enter inserts a newline.

The **Session** field holds a 16-char hex id; click **New** to start a
fresh conversation.

## Selecting a model

The web API doesn't expose a `model` field directly — models are bound
to *providers*, and the request body only accepts a `provider` **key**
that must already exist in the server's config. There are two ways to
pick a model:

### 1. Set the default at server start

```bash
# Override the default provider/model for the whole server
aar serve --provider anthropic --model claude-sonnet-4-6 --port 8080
aar serve --provider openai    --model gpt-4o-mini       --port 8080
aar serve --provider ollama    --model llama3            --port 8080
```

All requests made through the web UI then use that model. Leave the
**Provider** field in the UI blank.

### 2. Pre-configure named providers, switch per-request

Define a `providers` map in `~/.aar/config.json` once:

```json
{
  "provider": "sonnet",
  "providers": {
    "sonnet":  { "name": "anthropic", "model": "claude-sonnet-4-6" },
    "haiku":   { "name": "anthropic", "model": "claude-haiku-4-5" },
    "gpt4o":   { "name": "openai",    "model": "gpt-4o" },
    "gpt4om":  { "name": "openai",    "model": "gpt-4o-mini" },
    "llama":   { "name": "ollama",    "model": "llama3", "base_url": "http://localhost:11434" }
  }
}
```

Then put one of those keys (e.g. `gpt4om`, `llama`, `sonnet`) in the
UI's **Provider** field. It's sent as the per-request `provider`
override and the server resolves the matching model. The active model
appears in the status bar after each turn (it's read from the
`provider_meta` event).

If the key isn't found in config, the server silently falls back to its
default — so a misspelled key looks like it "worked" with the wrong
model. Check the status bar / Events panel to confirm.

## Events side panel

Click **Events ▸** in the header to slide a panel out on the right that
records every SSE event the server sent for the current session. This is
where `stream_chunk` lives — it's noisy (one frame per token) but useful
when debugging providers or tool plumbing. **Copy** dumps the whole
event log as JSON to the clipboard.
