# Providers

Aar is provider-agnostic — swap between Anthropic, OpenAI, Ollama, Gemini, or any OpenAI-compatible endpoint by changing one config field. No agent code changes required.

## Runtime provider switching

You can switch between providers mid-session without losing conversation history.

### Configuration

Define named providers in `config.json`:

```json
{
  "provider": "claude",
  "providers": {
    "claude": {
      "name": "anthropic", "model": "claude-sonnet-4-6",
      "context_window": 1000000, "token_budget": 500000, "cost_limit": 5.0
    },
    "gpt4": {
      "name": "openai", "model": "gpt-4o",
      "context_window": 200000, "token_budget": 500000, "cost_limit": 5.0
    },
    "local": {
      "name": "ollama", "model": "llama3", "base_url": "http://localhost:11434",
      "context_window": 32768, "token_budget": 0, "cost_limit": 0.0
    }
  }
}
```

Each provider profile can override `context_window`, `token_budget`, and `cost_limit`. These are model-coupled settings — context windows differ across models, and local models have no API cost. When a provider does not set these fields, the global values from `AgentConfig` apply as fallback.

The `provider` field can be a string key referencing `providers`, or an inline object (backward compatible).

### Slash command

All interactive transports (CLI, TUI, TUI Fixed) support the `/model` command:

| Command | Effect |
|---------|--------|
| `/model` | Show active provider and list available keys |
| `/model gpt4` | Switch to a named provider key |
| `/model openai/gpt-4o` | Ad-hoc switch by provider/model |

Switching is instant — the next turn uses the new provider. Conversation history is preserved because the internal event model is provider-agnostic.

### ACP

ACP stdio already supports `set_session_model` — it now also resolves named provider keys from the config. ACP HTTP accepts `provider` in `POST /runs` to select a named key.

### Web API

Pass `"provider": "gpt4"` in the request body of `POST /chat` or `POST /chat/stream` to use a named provider for that request.

### Programmatic

```python
from agent.core.agent import Agent
from agent.core.config import AgentConfig, ProviderConfig

config = AgentConfig(
    provider="claude",
    providers={
        "claude": ProviderConfig(name="anthropic", model="claude-sonnet-4-6"),
        "gpt4": ProviderConfig(name="openai", model="gpt-4o"),
    },
)
agent = Agent(config=config)
session = await agent.run("Hello from Claude", session=None)

# Switch mid-session
agent.switch_provider("gpt4")
session = await agent.run("Now using GPT-4o", session=session)

# Ad-hoc switch (no registry key needed)
agent.switch_provider("ollama/llama3")
```

## Anthropic

```python
from agent import AgentConfig, ProviderConfig

config = AgentConfig(provider=ProviderConfig(
    name="anthropic",
    model="claude-sonnet-4-20250514",
    api_key="sk-ant-...",         # or ANTHROPIC_API_KEY env var
))
```

Supports: tools, streaming, extended thinking (reasoning blocks).

## OpenAI

```python
config = AgentConfig(provider=ProviderConfig(
    name="openai",
    model="gpt-4o",
    api_key="sk-...",             # or OPENAI_API_KEY env var
))
```

Compatible with any OpenAI-compatible API (Azure, Together, etc.) via `base_url`.

## Ollama

```python
config = AgentConfig(provider=ProviderConfig(
    name="ollama",
    model="llama3.2",
    base_url="http://localhost:11434",   # default
    extra={"keep_alive": "10m"},
))
```

Enable reasoning extraction for models like `deepseek-r1`:

```python
ProviderConfig(name="ollama", model="deepseek-r1", extra={"supports_reasoning": True})
```

Enable vision for models with a vision encoder (see [Multimodal input](development.md#multimodal-input-images-audio-video)):

```python
ProviderConfig(name="ollama", model="qwen2.5vl:7b", extra={"supports_vision": True})
```

Audio note: Gemma 4 supports audio at the model level, but Ollama's API does not yet expose audio input (as of v0.20). Audio blocks attached via `@file` will be dropped with a warning. The framework types are ready for when Ollama adds support.

## Gemini

See [`docs/providers_gemini.md`](providers_gemini.md) for the full Gemini setup guide — SDK mode, HTTP mode, thinking/reasoning, and all `extra` keys.

Quick start:

```python
from agent import AgentConfig, ProviderConfig

config = AgentConfig(provider=ProviderConfig(
    name="gemini",
    model="gemini-2.5-flash",   # or "gemini-2.5-pro"
    api_key="...",               # or GEMINI_API_KEY env var
))
```

Install: `pip install aar-agent[gemini]` (pulls `google-genai`; HTTP mode only needs `httpx`, already included).

Supports: tools, streaming, thinking/reasoning (Flash optional, Pro default), vision.

## Generic (OpenAI-compatible)

Any OpenAI-compatible HTTP endpoint, using a custom `api-key` header for authentication.

```python
config = AgentConfig(provider=ProviderConfig(
    name="generic",
    model="gpt-4o-2024-08-06",
    api_key="...",           # or GENERIC_API_KEY env var
    extra={
        "endpoint": "https://api.provider.com/gpt/gpt-5.1",
        # Optional overrides:
        # "extra_headers": {"X-Trace-Id": "abc123"},
        # "timeout": 120.0,
        # "response_format": "json_object",  # "text" | "json_object" | "json_schema"
    },
))
```

The endpoint URL can also be set via the `GENERIC_ENDPOINT` environment variable.
Supports: tools, streaming, structured output (`json_object` / `json_schema`).

Install: `pip install aar-agent[generic]` (uses `httpx`, already included in the base install).

## Token reporting

Each provider reports token counts differently. Aar normalises them into a single `usage` dict `{"input_tokens": N, "output_tokens": M}` on the `ProviderMeta` event. See [Tokens, costs, and budgets](tokens.md) for how the counts flow through the system.

| Provider | Non-streaming | Streaming |
|----------|---------------|-----------|
| Anthropic | `usage` block in response body — always present | Collected from the `message_stop` SSE event; attached to the final `StreamDelta(done=True)` |
| OpenAI | `usage` in response body — always present | Requested via `stream_options: {include_usage: true}`; trailing usage chunk attached to final done-delta |
| Ollama | `prompt_eval_count` / `eval_count` in response body | Same fields on the final `done: true` NDJSON chunk; attached to final done-delta |
| Gemini | `usageMetadata` (HTTP) / `usage_metadata` (SDK) — always present | Same field on the final SSE chunk; attached to final done-delta. Thought tokens billed separately but not currently surfaced in `ProviderMeta`. |
| Generic | `usage` in SSE chunks if the upstream emits it | Same — presence depends on the upstream endpoint |

### Ollama token availability

Ollama includes `prompt_eval_count` and `eval_count` in its final streaming chunk for most models and versions. However, if a prompt hits the KV cache entirely, or if the model runtime omits these fields, the `usage` dict may arrive empty (`{}`). When the dict is empty:

- The `tui --fixed` header still shows `0in / 0out` (the counter starts at zero and simply doesn't increment).
- The `tui` body token line prints `0in / 0out` (if `token_usage.visible` is `true`).
- The `chat` transport suppresses the line entirely (it only prints when `usage` is non-empty).

No error is raised; cost is recorded as $0.00 for that step.

### Streaming is required for real-time counts

Token counts are only available once the provider's final chunk arrives. With `streaming: false` (the default), the complete response is returned in one shot and the count is available immediately after. With `streaming: true` the header in `tui --fixed` shows `streaming…` in the state field while the model generates, then snaps to the actual counts when the final chunk arrives. Enable streaming in your config:

```json
{
  "streaming": true
}
```

## Writing a new provider

Subclass `Provider` in `agent/providers/base.py` and implement `complete()`.
`stream()` has a default fallback so adapters without native streaming still
work — it calls `complete()` and replays the response as a short sequence of
deltas: one per text chunk, one per reasoning block, one per tool call, then
a terminal `StreamDelta(done=True, meta=response.meta)`.

The fallback is faithful: text, tool calls, reasoning, and `ProviderMeta` all
reach the stream consumer. Providers that implement `stream()` natively should
preserve the same invariants — exactly one `done=True` delta at the end, and
`meta` attached to that final delta so the loop can record usage.
