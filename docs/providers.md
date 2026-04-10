# Providers

Aar is provider-agnostic — swap between Anthropic, OpenAI, Ollama, or any OpenAI-compatible endpoint by changing one config field. No agent code changes required.

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
