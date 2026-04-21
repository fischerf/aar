# Gemini provider

The `gemini` provider adapts Google's **GenerateContent** wire format (Vertex AI / Gemini API) and operates in two modes selected automatically from config:

| Mode | When | Auth |
|------|------|------|
| **SDK** | `base_url` is empty | `api_key` passed to `google-genai` SDK |
| **HTTP** | `base_url` is set | `api-key` header (or configurable) sent via `httpx` |

Both modes support tools, streaming (SSE), vision, and reasoning (thinking tokens).

---

## Installation

```bash
pip install aar-agent[gemini]   # pulls google-genai + httpx
```

Or manually:

```bash
pip install google-genai httpx
```

The `google-genai` package is only required for **SDK mode**. HTTP mode uses `httpx`, which is already included in the base install.

---

## SDK mode — standard Google API

Use this when you have a standard Gemini API key from [Google AI Studio](https://aistudio.google.com/).

### Gemini 2.5 Flash

```python
from agent import AgentConfig, ProviderConfig

config = AgentConfig(provider=ProviderConfig(
    name="gemini",
    model="gemini-2.5-flash",
    api_key="...",           # or GEMINI_API_KEY env var
))
```

Thinking is **disabled by default** for Flash (`thinking_budget=0`). Enable it explicitly:

```python
ProviderConfig(
    name="gemini",
    model="gemini-2.5-flash",
    api_key="...",
    extra={"thinking_budget": 8000, "include_thoughts": True},
)
```

### Gemini 2.5 Pro

```python
config = AgentConfig(provider=ProviderConfig(
    name="gemini",
    model="gemini-2.5-pro",
    api_key="...",           # or GEMINI_API_KEY env var
))
```

Thinking is **enabled by default** for Pro (`thinking_budget=-1`, `include_thoughts=True`). The model decides its own budget. The TUI renders the thinking process live as a `▸ thinking` block.

To run Pro silently (no thought tokens returned, smaller responses):

```python
extra={"include_thoughts": False}
```

---

## HTTP mode — custom endpoint

Use this for any deployment that exposes the GenerateContent REST interface behind a custom URL (API gateways, enterprise proxies). No SDK package required.

Set `base_url` to the path ending with the **model slug**. The provider appends `:generateContent` and `:streamGenerateContentSse` automatically.

### Gemini 2.5 Flash

```python
config = AgentConfig(provider=ProviderConfig(
    name="gemini",
    model="gemini-2.5-flash",
    api_key="...",
    base_url="https://your-gateway.example.com/path/to/gemini/flash",
    extra={
        "auth_header": "api-key",   # header name for the API key
        "thinking_budget": 0,       # Flash: thinking off by default
        "include_thoughts": False,
    },
))
```

### Gemini 2.5 Pro

```python
config = AgentConfig(provider=ProviderConfig(
    name="gemini",
    model="gemini-2.5-pro",
    api_key="...",
    base_url="https://your-gateway.example.com/path/to/gemini/pro-thinking",
    extra={
        "auth_header": "api-key",
        "thinking_budget": -1,      # Pro: model-default budget
        "include_thoughts": True,   # surface thoughts in the TUI
        "timeout": 180.0,
    },
))
```

### Override individual endpoint URLs

If the gateway uses non-standard suffixes, override them explicitly:

```python
extra={
    "endpoint":        "https://gateway.example.com/model/v1/generate",
    "stream_endpoint": "https://gateway.example.com/model/v1/generate-stream",
}
```

---

## Thinking / reasoning

Gemini 2.5 models support a thinking budget — a token allowance the model may spend on internal reasoning before composing its response.

### How it works

1. Provider sends `thinkingConfig` in the request.
2. When `includeThoughts: true`, the API returns thought tokens as parts with `"thought": true`.
3. The provider emits them as `StreamDelta(reasoning_delta=...)`.
4. The TUI renders them as a `▸ thinking` inline block (streaming) or a `Thinking` panel (non-streaming).

### `thinking_budget` values

| Value | Meaning |
|-------|---------|
| `0` | Thinking disabled — no budget allocated, no thoughts returned |
| `-1` | Model-default — model decides its own budget (Pro default) |
| `N > 0` | Explicit cap — model may use up to N tokens for thinking |

### `include_thoughts`

Controls whether thought tokens are included in the API response.

| Value | Behaviour |
|-------|-----------|
| `True` (default when budget ≠ 0) | Thought parts returned; TUI renders them |
| `False` | Model still thinks (budget permitting) but thoughts are stripped from the response — saves output tokens |

> **Defaults by model:**
> - Flash — `thinking_budget=0`, `include_thoughts=False` (thinking off)
> - Pro — `thinking_budget=-1`, `include_thoughts=True` (thinking on, thoughts visible)

Both values can be overridden per-config via `extra`.

---

## `extra` keys reference

All keys are optional.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `thinking_budget` | `int` | `0` (Flash) / `-1` (Pro) | Token budget for thinking. `0` = off, `-1` = model default, `N` = explicit cap |
| `include_thoughts` | `bool` | `True` when budget ≠ 0 | Return thought tokens in the response for TUI rendering |
| `auth_header` | `str` | `"api-key"` | HTTP header name used to send the API key (HTTP mode only) |
| `endpoint` | `str` | `{base_url}:generateContent` | Full non-streaming endpoint URL override |
| `stream_endpoint` | `str` | `{base_url}:streamGenerateContentSse` | Full SSE streaming endpoint URL override |
| `timeout` | `float` | `120.0` | Per-request HTTP timeout in seconds |

---

## JSON config files

Ready-to-use samples are in `config/samples/`. Copy and set your `api_key`.

| File | Mode | Model | Thinking |
|------|------|-------|---------|
| `config_gemini_25_flash.json` | SDK | Flash | off |
| `config_gemini_25_pro.json` | SDK | Pro | on (visible) |
| `config_gemini_25_flash_custom.json` | HTTP | Flash | off |
| `config_gemini_25_pro_custom.json` | HTTP | Pro | on (visible) |

Minimal JSON for the HTTP / Pro case:

```json
{
  "provider": {
    "name": "gemini",
    "model": "gemini-2.5-pro",
    "api_key": "YOUR_KEY",
    "base_url": "https://your-gateway.example.com/path/to/gemini/pro-thinking",
    "max_tokens": 16384,
    "extra": {
      "auth_header": "api-key",
      "thinking_budget": -1,
      "include_thoughts": true,
      "timeout": 180.0
    }
  },
  "streaming": true,
  "context_window": 1000000
}
```

---

## Capabilities

| Feature | SDK mode | HTTP mode |
|---------|----------|-----------|
| Tools / function calling | ✓ | ✓ |
| Streaming (SSE) | ✓ | ✓ |
| Thinking / reasoning | ✓ | ✓ |
| Vision (image input) | ✓ | ✓ |
| Structured output | ✓ | ✓ |
| Token usage reporting | ✓ | ✓ |

---

## Token reporting

Token counts are returned in `usageMetadata` (HTTP) or `usage_metadata` (SDK) and normalised to the standard `ProviderMeta.usage` dict:

```python
{"input_tokens": N, "output_tokens": M, "total_tokens": T}
```

Note: `output_tokens` reflects **answer tokens only** — thought tokens are billed but reported separately by the API as `thoughtsTokenCount`. They are not currently surfaced in `ProviderMeta`; the raw value is available in the API response if needed.

---

## Environment variable

```bash
export GEMINI_API_KEY="your-key-here"
```

Fallback when `api_key` is not set in `ProviderConfig`. Applies to both SDK and HTTP modes.

---

*See also: [Providers overview](providers.md) · [Configuration reference](configuration.md) · [Tokens, costs, and budgets](tokens.md)*
