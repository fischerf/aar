# Development Guide

This document covers the programming API for building with Aar: multimodal input (images, audio, video), custom tools, the event model, sessions, cancellation, observability, and testing.

## Programmatic usage

```python
import asyncio
from agent import Agent, AgentConfig, ProviderConfig

config = AgentConfig(
    provider=ProviderConfig(name="anthropic", model="claude-sonnet-4-20250514"),
    system_prompt="You are a helpful coding assistant.",
)

agent = Agent(config=config)

async def main():
    session = await agent.run("List all Python files in the current directory")
    print(session.state)  # AgentState.COMPLETED

asyncio.run(main())
```

## Multimodal input (images, audio, video)

Aar supports multimodal input — images and audio — for capable models. Video support is **prepared** (types exist) but **not yet implemented** at the provider level.

### CLI / TUI — `@file` syntax

The easiest way to send images or audio from the CLI or TUI is the `@file` syntax:

```bash
# Image
aar run "What is in this photo? @photo.jpg"
aar chat   # then type: describe this @screenshot.png

# Audio (e.g. Gemma 4 E4B via Ollama)
aar run "Transcribe this clip @recording.wav"
aar chat   # then type: what do you hear? @clip.mp3

# Multiple attachments
aar run "Compare these @diagram.png @notes.wav"
```

Supported file types:
- **Image**: `.png`, `.jpg`/`.jpeg`, `.gif`, `.webp`, `.bmp`, `.tiff`
- **Audio**: `.wav`, `.mp3`, `.ogg`, `.flac`, `.m4a`
- **Video**: `.mp4`, `.webm`, `.mov` *(prepared — raises an error until provider support is added)*

Files are read, base-64 encoded, and sent as typed content blocks. Media blocks are placed **before** text in the content list for optimal model performance (recommended by Gemma 4 docs).

### Programmatic API

Pass a list of `ContentBlock` objects instead of a plain string to `Agent.run()`, `Agent.chat()`, or `Session.add_user_message()`.

```python
from agent.core.events import TextBlock, ImageURLBlock, ImageURL, AudioBlock, AudioData

# Image — HTTP URL
response = await agent.chat([
    ImageURLBlock(image_url=ImageURL(url="https://example.com/diagram.png")),
    TextBlock(text="What is shown in this diagram?"),
])

# Image — local file via base-64 data URI
import base64
raw = open("screenshot.png", "rb").read()
data_uri = "data:image/png;base64," + base64.b64encode(raw).decode()
response = await agent.chat([
    ImageURLBlock(image_url=ImageURL(url=data_uri)),
    TextBlock(text="Describe this screenshot."),
])

# Audio — base-64 data URI
raw = open("clip.wav", "rb").read()
data_uri = "data:audio/wav;base64," + base64.b64encode(raw).decode()
response = await agent.chat([
    AudioBlock(audio=AudioData(url=data_uri, format="wav")),
    TextBlock(text="What do you hear?"),
])

# OpenAI vision detail hint ("auto" | "low" | "high")
ImageURLBlock(image_url=ImageURL(url="https://example.com/photo.jpg", detail="high"))
```

Or use the helper to convert local files automatically:

```python
from agent.core.multimodal import file_to_content_block, parse_multimodal_input
from pathlib import Path

# Single file
block = file_to_content_block(Path("photo.jpg"))  # → ImageURLBlock
block = file_to_content_block(Path("clip.wav"))    # → AudioBlock

# Parse user input with @file syntax
content = parse_multimodal_input("Describe @photo.jpg")
# Returns list[ContentBlock] if attachments found, or str if plain text
session = await agent.run(content)
```

Text-only callers are completely unchanged — passing a plain string still works.

### Provider support

| Provider | Vision | Audio | Notes |
|---|---|---|---|
| Anthropic | always | — | claude-3+ models; HTTP URLs and base-64 data URIs |
| OpenAI | auto-detected | — | gpt-4o, gpt-4-vision, o1 and newer |
| Ollama | default on | **not yet** | Vision works; audio not yet exposed in Ollama's API (as of v0.20) |
| Generic | auto-detected | — | Any OpenAI-compatible endpoint |

For Ollama, vision defaults to `True`:

```python
ProviderConfig(
    name="ollama",
    model="gemma4:e4b",
    extra={"supports_vision": True},   # default True; set False to opt out
)
```

> **Audio note:** Gemma 4 supports audio at the model level, but Ollama's `/api/chat` endpoint does not accept audio data as of v0.20. Audio blocks attached via `@file` will be **dropped with a warning**. The framework `AudioBlock` type is kept so pipelines are ready when Ollama adds audio support.

Check capabilities at runtime:

```python
print(agent.provider.supports_vision)  # True / False
print(agent.provider.supports_audio)   # True / False (always False for Ollama currently)
```

### Video support (prepared, not yet implemented)

The `VideoBlock` and `VideoData` types exist in `agent.core.events` so you can start building pipelines, but passing video to any provider will currently raise `ValueError`. This will be implemented once Ollama and other providers have stable video input APIs.

```python
from agent.core.events import VideoBlock, VideoData

# Type exists for future use
block = VideoBlock(video=VideoData(url="data:video/mp4;base64,...", format="mp4"))

# But file_to_content_block raises ValueError for video files
from agent.core.multimodal import file_to_content_block
file_to_content_block(Path("clip.mp4"))  # ValueError: Video input is prepared but not yet implemented
```

### Format conversion

The same `ContentBlock` API works identically across all providers. Aar converts internally:

- **OpenAI / Generic** — content blocks forwarded as-is (already the OpenAI wire format)
- **Anthropic** — `image_url` blocks converted to `{"type": "image", "source": {...}}`; `data:` URIs become `base64` sources, HTTP URLs become `url` sources
- **Ollama** — uses the native `/api/chat` format: text in `content` (string), base-64 payloads in top-level `images` list. Audio blocks are not yet supported and are dropped with a warning

### Ollama multimodal models

Pull any multimodal model and point the provider at it:

```bash
# Vision + audio (Gemma 4)
ollama pull gemma4:e4b
aar chat --provider ollama --model gemma4:e4b

# Vision only
ollama pull qwen2.5vl:7b
aar chat --provider ollama --model qwen2.5vl:7b
```

Audio note: Gemma 4 E4B supports audio at the model level, but Ollama's API does not yet expose audio input (as of v0.20). Audio files attached via `@file` will be dropped with a warning. This will work automatically once Ollama adds audio support.

### Multi-turn with media

Images and audio in earlier turns are preserved in `session.to_messages()` — subsequent text-only turns can refer back to them:

```python
session = None

session = await agent.run(
    [ImageURLBlock(image_url=ImageURL(url="https://example.com/mockup.png")),
     TextBlock(text="Here is our UI mockup.")],
    session=session,
)
session = await agent.run("Now write the HTML for it.", session=session)
```

### Web API (serve mode)

The web transport (`POST /chat`) accepts multimodal content via the standard content block format in JSON. Clients send base-64 data URIs or HTTP URLs directly — the `@file` syntax is a CLI/TUI convenience only.

```json
{
  "prompt": [
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBOR..."}},
    {"type": "audio", "audio": {"url": "data:audio/wav;base64,UklG...", "format": "wav"}},
    {"type": "text", "text": "Describe what you see and hear."}
  ]
}
```

### Accessing content blocks directly

```python
from agent.core.events import TextBlock, ImageURLBlock, ImageURL, AudioBlock, AudioData, ContentBlock

# Build a typed block list
parts: list[ContentBlock] = [
    ImageURLBlock(image_url=ImageURL(url="https://example.com/chart.png", detail="high")),
    AudioBlock(audio=AudioData(url="data:audio/wav;base64,...", format="wav")),
    TextBlock(text="Analyse this chart and audio."),
]

# Session helper
from agent.core.session import Session
s = Session()
msg = s.add_user_message(parts)
print(msg.is_multimodal)   # True
print(msg.content)         # "Analyse this chart and audio."  (text summary for logging)
```

## Tool system

### Built-in tools

| Tool | Side effect | Description |
|---|---|---|
| `read_file` | read | Read a file with line numbers |
| `write_file` | write | Write a file, creating directories as needed |
| `edit_file` | write | Replace an exact string in a file (must be unique) |
| `list_directory` | read | List files and directories |
| `bash` | execute | Run a shell command, return stdout + stderr |

All built-ins are opt-in via `ToolConfig.enabled_builtins`.

### Custom tools

```python
from agent import Agent
from agent.tools.schema import SideEffect, ToolSpec

agent = Agent()

# Decorator style
@agent.registry.register(
    name="fetch_url",
    description="Fetch the contents of a URL",
    side_effects=[SideEffect.NETWORK],
)
async def fetch_url(url: str) -> str:
    import httpx
    async with httpx.AsyncClient() as client:
        r = await client.get(url)
        return r.text

# Or explicit ToolSpec
agent.registry.add(ToolSpec(
    name="count_lines",
    description="Count the lines in a file",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
    side_effects=[SideEffect.READ],
    handler=lambda path: str(sum(1 for _ in open(path))),
))
```

## Sessions and persistence

Sessions are automatically saved as JSONL files. Every event (messages, tool calls, results, metadata) is persisted. Each session carries a `session_id`, a `run_id` (refreshed on resume), and a `trace_id` (stable for the lifetime of the session object).

```python
from agent import Agent
from agent.memory.session_store import SessionStore

agent = Agent()
store = SessionStore(".agent/sessions")

# First run
session = await agent.run("Write a Python script that sorts a CSV")
store.save(session)
print(session.session_id)  # e.g. "a3f1b2c4d5e6f7a8"
print(session.trace_id)    # stable identifier for logging / tracing

# Resume later
session = store.load("a3f1b2c4d5e6f7a8")
session = await agent.run("Now add error handling", session=session)

# List all sessions
print(store.list_sessions())

# Compact a long session to its most recent 200 events
store.compact("a3f1b2c4d5e6f7a8", max_events=200)
```

From the CLI, resume a session by passing `--session` (or `-s`) to any interactive command:

```bash
aar chat --session a3f1b2c4d5e6f7a8
aar run "add error handling" --session a3f1b2c4d5e6f7a8
aar tui --session a3f1b2c4d5e6f7a8
```

## Event model

The agent emits typed events you can subscribe to:

```python
from agent.core.events import AssistantMessage, ToolCall, ToolResult, EventType

def on_event(event):
    if isinstance(event, ToolCall):
        print(f"-> {event.tool_name}({event.arguments})")
    elif isinstance(event, ToolResult) and event.is_error:
        print(f"FAIL {event.tool_name}: {event.output}")
    elif isinstance(event, AssistantMessage):
        print(event.content)

agent.on_event(on_event)
session = await agent.run("Do something")
```

Event types: `user_message`, `assistant_message`, `tool_call`, `tool_result`, `reasoning`, `provider_meta`, `error`, `session`.

Timing fields are populated automatically by the runtime:
- `ProviderMeta.duration_ms` — wall time for the provider API call
- `ToolResult.duration_ms` — wall time for tool execution

## Cancellation

Pass an `asyncio.Event` to stop the loop cooperatively between steps:

```python
import asyncio
from agent.core.loop import run_loop

cancel = asyncio.Event()

# Cancel from another coroutine or thread
asyncio.get_event_loop().call_later(5.0, cancel.set)

session = await run_loop(session, provider, executor, config, cancel_event=cancel)
# session.state == AgentState.CANCELLED
```

Hard cancellation via `asyncio` task cancellation also works — the loop catches `CancelledError`, sets state to `CANCELLED`, and re-raises.

## Observability

Aggregate timing and token usage from any session:

```python
from agent.extensions.observability import session_metrics

m = session_metrics(session)
print(f"steps={m.total_steps}")
print(f"tokens={m.total_tokens}  (in={m.total_input_tokens} out={m.total_output_tokens})")
print(f"provider_ms={m.total_provider_duration_ms:.0f}")
print(f"tool_ms={m.total_tool_duration_ms:.0f}  calls={m.total_tool_calls}")
print(f"errors={m.total_errors}")

# Per-step breakdown
for step in m.steps:
    print(f"  step {step.step}: provider={step.provider_duration_ms:.0f}ms  tools={step.total_tool_duration_ms:.0f}ms")
```

`session_metrics()` reads all events once; it does not require a live provider or executor.

## Testing

```bash
pip install "aar-agent[dev]"
pytest tests/ -v
```

The test suite runs entirely without live API calls using a `MockProvider`. Tests cover:

- Loop termination, max steps, timeout, cancellation (`asyncio.Event` + `CancelledError`), provider errors
- Session persistence, resumption, compaction, `trace_id` round-trip, message conversion
- Event serialization round-trips for all event types, including `duration_ms` fields
- Multimodal content blocks (image, audio, video), input parsing, file-to-block conversion, Ollama native format
- Provider normalization for Anthropic, OpenAI, and Ollama (mocked)
- Tool registry, schema inference, execution (sync/async, timeout, truncation, timing)
- Safety policy (command deny-list, path restrictions, read-only mode, approval gates)
- Sandbox execution and timeout
- `session_metrics()` aggregation (timing, tokens, errors, per-step breakdown)
- MCP bridge: tool discovery, handler dispatch, content serialization, name collision detection, stdio/http transports (all mocked — no real MCP server required)

### Live testing against real providers

Live tests hit actual provider APIs and are skipped by default. Pass `--live` to enable them.

#### Ollama (local, no API key required)

```bash
# Pull a model first
ollama pull qwen3.5:9b

# Run the live CLI tests
pytest tests/test_cli.py -m live --live -v
```

The live test class (`TestLiveOllama`) uses `qwen3.5:9b` by default. To use a different model, edit the `MODEL` constant in `tests/test_cli.py` or run a one-off check via the CLI:

```bash
aar run "Reply with the word PONG." --provider ollama --model llama3.2
```

#### Anthropic

```bash
export ANTHROPIC_API_KEY=sk-ant-...
pytest tests/test_providers.py -m live --live -k Anthropic -v
```

Uses `claude-haiku-4-5-20251001` by default (cheapest model). Quick smoke-test via CLI:

```bash
aar run "Reply with the word PONG." --provider anthropic --model claude-haiku-4-5-20251001
```

#### OpenAI (or any OpenAI-compatible endpoint)

```bash
export OPENAI_API_KEY=sk-...
pytest tests/test_providers.py -m live --live -k OpenAI -v
```

Uses `gpt-4o-mini` by default.

#### Running all live tests together

```bash
# All providers (Anthropic + OpenAI + Ollama CLI tests)
pytest tests/ -m live --live -v

# Single provider
pytest tests/test_providers.py -m live --live -k Anthropic -v
pytest tests/test_providers.py -m live --live -k OpenAI -v
pytest tests/test_cli.py -m live --live -v           # Ollama
```

Tests for providers whose API key is not set will fail with an authentication error rather than being skipped — only export keys for the providers you want to exercise.
