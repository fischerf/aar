# Token Tracking, Cost Estimation, and Budget Enforcement

Aar tracks token usage end-to-end: from the raw provider response through session
accumulation, cost estimation, visual display, and hard budget enforcement. This
page documents every stage of that pipeline.

> **See also:** [`configuration.md`](configuration.md) for the full `AgentConfig`
> reference, and [`providers.md`](providers.md) for provider setup and credentials.

---

## 1. Overview

Every time a provider returns a response (streamed or not), Aar captures the token
counts reported by that provider, attaches them to a `ProviderMeta` event, and
accumulates them on the current `Session`. The session totals drive two independent
checks — a token-count budget and a cost-in-USD limit — both evaluated after each
provider call. Estimated cost is loaded from `agent/core/pricing.json` (shipped
with the package), optionally merged with `~/.aar/pricing.json` (user override),
using longest-prefix model-name matching. All four
transports (CLI `chat`, inline TUI, full-screen TUI, web) surface these counts to
the user; the TUIs additionally show colour-coded warnings as limits approach.
Local and Ollama models that don't match any pricing-table entry report `$0.00`.

---

## 2. Token Tracking Pipeline

### 2.1 Streaming path

```
provider.stream()
    │
    ├─ StreamDelta(done=False, text="…")   ← no token data
    ├─ StreamDelta(done=False, text="…")   ← no token data
    │   …
    └─ StreamDelta(done=True, meta=ProviderMeta(usage={…}))
                                │
                        _consume_stream()   ← in provider_runner.py
                                │
                        returns ProviderResponse(meta=ProviderMeta(…))
                                │
                        run_loop stamps duration_ms on meta
                                │
                        emits ProviderMeta as event
                                │
                        session.accumulate(meta)
                                │
                        budget / cost enforcement check
```

Each `StreamDelta` carries plain text. Token counts are present **only** on the
final delta where `done=True`. `_consume_stream()` extracts `delta.meta` from
that terminal delta and returns it as part of `ProviderResponse.meta`. `run_loop`
then stamps `duration_ms` on the meta object before emitting it as a named event.

### 2.2 Non-streaming path

```
provider.complete()
    │
    └─ ProviderResponse(meta=ProviderMeta(usage={…}))
                                │
                        run_loop stamps duration_ms on meta
                                │
                        emits ProviderMeta as event
                                │
                        session.accumulate(meta)
                                │
                        budget / cost enforcement check
```

`provider.complete()` returns a fully-populated `ProviderResponse` directly. The
remainder of the pipeline (emit → accumulate → enforce) is identical to the
streaming path.

### 2.3 The `usage` dict format

`ProviderMeta.usage` is a plain `dict[str, int]` with at minimum:

| Key              | Meaning                    |
|------------------|----------------------------|
| `input_tokens`   | Tokens in the prompt       |
| `output_tokens`  | Tokens in the completion   |

Providers may include additional keys (e.g. cache breakdowns). An empty dict
(`{}`) means the provider did not report counts for this call (see §3 for the
Ollama caveat).

---

## 3. Provider Token Reporting

### 3.1 Summary table

| Provider  | Non-streaming                                       | Streaming                                                                                     |
|-----------|-----------------------------------------------------|-----------------------------------------------------------------------------------------------|
| Anthropic | `usage` block in the response body                  | Collected at the `message_stop` SSE event; attached to the final `StreamDelta(done=True)`    |
| OpenAI    | `usage` block in the response body                  | Requested via `stream_options: {include_usage: true}`; trailing usage chunk on final delta   |
| Ollama    | `prompt_eval_count` / `eval_count` in response body | Same fields on the final `done: true` chunk; attached to the final `StreamDelta(done=True)`  |
| Generic   | `usage` block in response body if present           | `usage` from SSE chunks if present; attached to the final `StreamDelta(done=True)`           |

### 3.2 Per-provider notes

**Anthropic** — Usage is reliably present in every response. Cache token breakdowns
(`cache_read_input_tokens`, `cache_creation_input_tokens`) are included when
prompt caching is active. These are captured but not yet surfaced in the default
display.

**OpenAI** — Aar explicitly opts in to usage reporting on streamed responses by
sending `stream_options: {include_usage: true}`. Without this, OpenAI omits usage
from streaming responses entirely.

**Ollama** — Reports `prompt_eval_count` (input) and `eval_count` (output). These
fields are only present when the model runtime performs actual evaluation. If the
entire prompt is served from KV-cache and the runtime elides the fields, `usage`
will be `{}` (empty dict). In that case token counts are treated as zero for that
turn — no budget is deducted and no display line is rendered.

**Generic** — Best-effort: the adapter forwards whatever `usage` key is present in
the response body or SSE chunks. If the endpoint does not include usage data,
`usage` will be `{}`.

---

## 4. Token Display by Transport

### 4.1 `chat` (CLI)

A dim, right-aligned usage line is printed after each assistant response:

```
                                                      (150in / 80out)
```

- Rendered only when `ProviderMeta.usage` is a **non-empty** dict.
- There is no config option to suppress it in `chat` mode.

### 4.2 `tui` (inline Rich scrollable)

A per-turn token line (dim, right-aligned) is appended after each assistant
response block, using the same `Xin / Yout` format.

**Configuration:**

```json
"tui": {
  "layout": {
    "token_usage": {
      "visible": true
    }
  }
}
```

Set `"visible": false` to hide the per-turn token line entirely. When hidden, the
session totals are still accumulated — only the display is suppressed.

### 4.3 `tui --fixed` (Textual full-screen)

This transport has two independent token surfaces:

#### Header bar (always visible)

The header permanently displays cumulative session totals:

```
 aar  running   tokens: 1 240in / 620out  $0.0182
```

- Updated after every `ProviderMeta` event fires.
- **Not** affected by `token_usage.visible` — it is always shown.
- Cost is displayed alongside tokens when a matching pricing entry exists.

#### Streaming state lifecycle

During a streaming response the `state` field in the header transitions through
the following stages:

```
 idle
   │
   │  first StreamDelta received
   ▼
 streaming…          ← header state field changes to "streaming…"
   │
   │  StreamChunk(finished=True) fires (stream closed)
   ▼
 running             ← state reverts to current AgentState label
   │
   │  ProviderMeta event fires
   ▼
 running             ← header token/cost counts snap to new cumulative total
```

The `streaming…` label is a visual hint only; it does not map to an `AgentState`
value.

#### Body token line

The same per-turn `Xin / Yout` line as `tui` mode. Also controlled by
`token_usage.visible`. Hiding it does not affect the header bar counts.

---

## 5. Session Accumulation

`Session` holds cumulative counters for the lifetime of a single `agent.run()`
call:

| Field                 | Type    | Description                                    |
|-----------------------|---------|------------------------------------------------|
| `total_input_tokens`  | `int`   | Sum of all `input_tokens` across every step    |
| `total_output_tokens` | `int`   | Sum of all `output_tokens` across every step   |
| `total_cost`          | `float` | Estimated USD cost accumulated across all steps|

**Derived property:**

```
session.total_tokens == session.total_input_tokens + session.total_output_tokens
```

**Accumulation timing:** totals are updated immediately after every provider call,
before the budget/cost enforcement checks run. This means enforcement always sees
the up-to-date totals.

**Reset behaviour:** the `Session` object is created fresh at the start of each
`agent.run()` call. Limits therefore apply per-run, not across the lifetime of the
`Agent` instance.

**Session restore:** when resuming a persisted session (e.g. via `aar chat --resume`
or the ACP `session/load` endpoint), token and cost tallies (`total_input_tokens`,
`total_output_tokens`, `total_cost`) are restored from the saved state. This means
budget enforcement continues from where the previous run left off rather than
resetting to zero.

---

## 6. Cost Estimation

### 6.1 Pricing table

Pricing is loaded from two JSON files merged in order (later overrides earlier):

1. **`agent/core/pricing.json`** — shipped with the package; the built-in baseline.
2. **`~/.aar/pricing.json`** — optional user override; loaded if the file exists.

The format is a flat JSON object whose keys are model-name **prefixes** and values
hold four price fields (all USD per 1 million tokens):

| Field                    | Meaning                          |
|--------------------------|----------------------------------|
| `input_per_million`      | Standard input token price       |
| `output_per_million`     | Standard output token price      |
| `cache_read_per_million` | Cache-read discount price        |
| `cache_write_per_million`| Cache-write surcharge price      |

Keys starting with `_` (e.g. `"_comment"`) are ignored and may be used as inline
comments. Example:

```json
{
  "_comment": "USD per 1M tokens. Keys are model-name prefixes.",
  "claude-sonnet-4": { "input_per_million": 3.0, "output_per_million": 15.0, "cache_read_per_million": 0.30, "cache_write_per_million": 3.75 },
  "gemma4": { "input_per_million": 0.05, "output_per_million": 0.10, "cache_read_per_million": 0.0, "cache_write_per_million": 0.0 }
}
```

Built-in entries as of the initial release include: `claude-sonnet-4`, `claude-opus-4`,
`claude-3-5-haiku`, `claude-3-5-sonnet`, `gpt-4o`, `gpt-4o-mini`, `gpt-4.1`,
`gpt-4.1-mini`, `gpt-4.1-nano`, `o3`, `o3-mini`, `o4-mini`.

`aar init` writes `~/.aar/pricing.template.json` — a copy of the full built-in
pricing table — as a starting point for customisation. Copy it to
`~/.aar/pricing.json` and edit as needed.

To invalidate the in-process pricing cache (useful in tests or after editing the
file at runtime), call `reload_pricing_table()` from `agent.core.tokens`.

### 6.2 Prefix matching

`get_pricing(model)` iterates the pricing table sorted **longest prefix first**,
returning the first entry whose key is a prefix of the supplied model string. This
means a model name like `claude-sonnet-4-20250514` matches the `claude-sonnet-4`
key correctly, and a hypothetical `claude-sonnet-4-5-turbo` would match
`claude-sonnet-4` (not a shorter key) if no more-specific key existed.

### 6.3 Limitations

- Prices are **approximate** and reflect public pricing pages as of mid-2025.
- No caching discounts, batching tiers, or enterprise pricing are modelled.
- Future price changes require editing `agent/core/pricing.json` or adding
  overrides to `~/.aar/pricing.json`.
- Cache token costs (`cache_read_tokens`, `cache_write_tokens`) are included in
  the calculation when reported by the provider but are not yet broken out in the
  default display.

### 6.4 Local / Ollama models

Models whose name does not match any prefix in the pricing table are assigned
`None` pricing. Cost is displayed as `$0.00` and `total_cost` remains `0.0`.
`cost_limit` enforcement is effectively disabled for these models (the limit is
never reached).

---

## 7. Warning Thresholds

Two fractional thresholds control when the counters switch to a warning style:

| Threshold                 | Default | Applies to                                                     |
|---------------------------|---------|----------------------------------------------------------------|
| `token_warning_threshold` | `0.8`   | `session.total_tokens >= token_budget * threshold`             |
| `cost_warning_threshold`  | `0.8`   | `session.total_cost >= cost_limit * threshold`                 |

When a threshold is crossed:

- **Inline TUI (`tui`):** the per-turn token line switches to
  `usage_warning_style` (theme key, default `bold red`).
- **Full-screen TUI (`tui --fixed`):** the header token/cost counter switches to
  `tokens_warning_style` (header styles key, default `bold red`).

Warnings are **visual only**. The agent continues running normally until the hard
limit is reached. Thresholds have no effect when the corresponding limit is `0`
(unlimited).

---

## 8. Hard Limits and Enforcement

Both checks run inside `run_loop` after each provider call and session
accumulation step, before the next iteration begins.

### 8.1 Token budget

```
if token_budget > 0 and session.total_tokens >= token_budget:
    emit ErrorEvent(message="Token budget exceeded (X/Y)", recoverable=False)
    set AgentState.BUDGET_EXCEEDED
    return  # loop terminates
```

### 8.2 Cost limit

```
if cost_limit > 0.0 and session.total_cost >= cost_limit:
    emit ErrorEvent(message="Cost limit exceeded ($X/$Y)", recoverable=False)
    set AgentState.BUDGET_EXCEEDED
    return  # loop terminates
```

### 8.3 Outcome

| Event / state          | Value                                           |
|------------------------|-------------------------------------------------|
| `AgentState`           | `BUDGET_EXCEEDED`                               |
| `ErrorEvent.message`   | `"Token budget exceeded (X/Y)"` or cost variant |
| `ErrorEvent.recoverable` | `False`                                       |

The loop returns immediately after setting the state. No further tool calls or
provider calls are made. The session totals at the point of termination are
preserved and remain readable.

---

## 9. Configuration Reference

All fields live on `AgentConfig`. See [`configuration.md`](configuration.md) for
the full config schema.

| Field                     | Type    | Default | Meaning                                                       |
|---------------------------|---------|---------|---------------------------------------------------------------|
| `token_budget`            | `int`   | `0`     | Maximum total tokens per run. `0` = unlimited.                |
| `cost_limit`              | `float` | `0.0`   | Maximum estimated USD cost per run. `0.0` = unlimited.        |
| `token_warning_threshold` | `float` | `0.8`   | Fraction of `token_budget` at which warning style activates.  |
| `cost_warning_threshold`  | `float` | `0.8`   | Fraction of `cost_limit` at which warning style activates.    |
| `streaming`               | `bool`  | `False` | Enable streaming path. Recommended for interactive transports.|

---

## 10. Layout Config Quick Reference

These keys live under `tui.layout` (inline TUI) or `tui_fixed.header_styles`
(full-screen TUI) in the JSON config. Theme-level style keys are set under
`tui.theme` or `tui_fixed.theme`.

| Config path                              | Transport     | Default      | Effect                                                              |
|------------------------------------------|---------------|--------------|---------------------------------------------------------------------|
| `tui.layout.token_usage.visible`         | `tui`         | `true`       | Show/hide the per-turn `Xin / Yout` line in the scrollable TUI.    |
| `tui.layout.token_usage.visible`         | `tui --fixed` | `true`       | Show/hide the per-turn body token line (header is unaffected).      |
| `tui.theme.usage_style`                  | `tui`         | `dim`        | Rich style applied to the token line under normal conditions.       |
| `tui.theme.usage_warning_style`          | `tui`         | `bold red`   | Rich style applied to the token line when a warning threshold fires.|
| `tui_fixed.header_styles.tokens_style`   | `tui --fixed` | `dim`        | Rich style for the header token/cost counter under normal conditions.|
| `tui_fixed.header_styles.tokens_warning_style` | `tui --fixed` | `bold red` | Rich style for the header counter when a warning threshold fires. |

> Style values use [Rich markup syntax](https://rich.readthedocs.io/en/stable/style.html)
> (e.g. `"bold red"`, `"dim cyan"`, `"italic on dark_blue"`).
> See [`themes.md`](themes.md) for the full theme reference.