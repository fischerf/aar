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

Enable vision for models with a vision encoder (see [Image input](development.md#image-input-multimodal)):

```python
ProviderConfig(name="ollama", model="qwen2.5vl:7b", extra={"supports_vision": True})
```

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
