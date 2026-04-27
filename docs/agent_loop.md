# Agent Loop & Guardrails

The core execution loop lives in `agent/core/loop.py`. Guardrails logic is
isolated in `agent/core/guardrails.py` and configured via `GuardrailsConfig`
inside `AgentConfig`.

---

## Loop Flow

```
User message
     │
     ▼
┌─────────────────────────────────────────────────────────────────┐
│                        LOOP ITERATION                           │
│                                                                 │
│  ┌─ Pre-flight checks ───────────────────────────────────────┐  │
│  │  • cancel_event set?  → CANCELLED                         │  │
│  │  • elapsed > timeout? → TIMED_OUT  (skipped if timeout=0)  │  │
│  │  • step_count ≥ max_steps? → MAX_STEPS                    │  │
│  └───────────────────────────────────────────────────────────┘  │
│                           │                                     │
│                           ▼                                     │
│  ┌─ Context management ──────────────────────────────────────┐  │
│  │  context_window > 0 & strategy="sliding_window"           │  │
│  │    → trim_to_token_budget(messages, context_window)       │  │
│  └───────────────────────────────────────────────────────────┘  │
│                           │                                     │
│                           ▼                                     │
│  ┌─ Provider request (with retries) ─────────────────────────┐  │
│  │  • streaming or complete()                                │  │
│  │  • exponential back-off on recoverable errors             │  │
│  │  • max_retries attempts before ERROR state                │  │
│  └───────────────────────────────────────────────────────────┘  │
│                           │                                     │
│                           ▼                                     │
│  ┌─ Budget accounting ───────────────────────────────────────┐  │
│  │  • accumulate input/output tokens → session.total_tokens  │  │
│  │  • calculate cost → session.total_cost                    │  │
│  │  • token_budget exceeded? → BUDGET_EXCEEDED               │  │
│  │  • cost_limit exceeded?   → BUDGET_EXCEEDED               │  │
│  └───────────────────────────────────────────────────────────┘  │
│                           │                                     │
│            ┌──────────────┴────────────────┐                    │
│            │ tool_calls present?           │                   │
│           YES                             NO                   │
│            │                               │                   │
│            ▼                              ▼                   │
│  ┌─ GUARDRAIL ──────────┐    ┌─ stop_reason? ───────────────┐  │
│  │ Repetition detection │    │ END_TURN / MAX_TOKENS        │  │
│  │                      │    │                              │  │
│  │ observe_tool_calls() │    │ MAX_TOKENS + recoveries left?│  │
│  │ is_stuck()?          │    │   YES → inject continuation  │  │
│  │   YES → ERROR state  │    │         message & continue   │  │
│  │   NO  → execute tools│    │   NO  → done = True          │  │
│  └──────────────────────┘    └──────────────────────────────┘  │
│            │                              │                    │
│            ▼                              │                    │
│      loop continues ◄─────────────────────┘                    │
└────────────────────────────────────────────────────────────────┘
                           │
                           ▼
                   COMPLETED / ERROR
```

---

## Guardrails Reference

Guardrails are **mechanical safety nets** — they catch runaway loops and
truncated responses without needing the LLM to self-regulate.

All mutable counters live in `session.metadata["guardrails"]` so they are
automatically persisted and restored across session reloads.

### 1. Max-tokens recovery

| Config key | Default | Effect |
|---|---|---|
| `max_tokens_recoveries` | `2` | How many times a `max_tokens` truncation is auto-recovered |
| `reserve_tokens` | `512` | If remaining tokens ≤ this, `near_budget()` returns True |

**Behaviour:**

```
Model stops with stop_reason = "max_tokens"
  └─ recovery_count < max_tokens_recoveries?
       YES → inject continuation prompt + loop again
             "Continue from exactly where you left off…"
       NO  → treat as normal END_TURN, exit loop
```

The injected prompt (`guardrails.max_tokens_followup()`) is added as an
internal user message (tagged `data["reason"] = "max_tokens_recovery"`) and is
**not** shown in the UI.

### 2. Repetition / stuck-loop detection

| Config key | Default | Effect |
|---|---|---|
| `max_repeated_tool_steps` | `3` | Consecutive identical tool-call sets before the loop aborts |

**Behaviour:**

```
Each step with tool calls:
  observe_tool_calls(session, tool_calls)
    → compute deterministic signature (tool names + argument key=value pairs)
    → same as last step?  repeated_tool_steps += 1
    → different?          repeated_tool_steps  = 0, update signature

  is_stuck()?
    repeated_tool_steps ≥ max_repeated_tool_steps
      YES → emit ErrorEvent, set state = ERROR, return session
```

The signature includes argument **values** (truncated at 200 chars) so calling
the same tool on different files is not counted as repetition.

### 3. Budget proximity

| Config key | Default | Effect |
|---|---|---|
| `reserve_tokens` | `512` | Token headroom before `near_budget()` fires |
| `reserve_cost_fraction` | `0.1` | Fraction of `cost_limit` treated as reserve |

`near_budget()` is available for callers that want to warn or slow down before
hitting hard limits. Hard budget enforcement (`token_budget`, `cost_limit`) is
handled directly in the loop — the agent exits with `BUDGET_EXCEEDED` state.

---

## Config section

```json
"guardrails": {
  "max_tokens_recoveries":   2,    // auto-retry truncated responses (0 = off)
  "max_repeated_tool_steps": 3,    // consecutive identical tool calls before abort
  "reserve_tokens":          512,  // near_budget() token headroom
  "reserve_cost_fraction":   0.1   // near_budget() cost headroom (fraction of cost_limit)
}
```

### Tuning by use case

| Use case | Suggested adjustments |
|---|---|
| Cloud provider, tight budget | Lower `reserve_cost_fraction` to `0.15–0.2` to warn earlier |
| Long reasoning models (DeepSeek) | Raise `max_tokens_recoveries` to `3` — truncation is more likely |
| Autonomous/high-step runs | Keep `max_repeated_tool_steps` at `3`; lower only if you trust the model |
| Local models, no cost limit | `reserve_cost_fraction` has no effect (cost_limit = 0) |

---

## State transitions

```
                 ┌──────────────────────────────────────────────┐
                 │             AgentState                       │
                 │                                              │
   start ──────► RUNNING ──────────────────────► COMPLETED     │
                 │                                              │
                 ├── cancel_event set ────────► CANCELLED       │
                 ├── elapsed > timeout > 0 ──► TIMED_OUT       │
                 ├── step_count ≥ max_steps ──► MAX_STEPS       │
                 ├── budget exceeded ─────────► BUDGET_EXCEEDED │
                 ├── repetition guard ────────► ERROR           │
                 ├── provider error (fatal) ──► ERROR           │
                 │                                              │
                 ├─────► WAITING_FOR_TOOL ───────┐              │
                 │       (tool execution)        │              │
                 └───────────────────────────────┘              │
                 │                                              │
                 ├─────► WAITING_FOR_INPUT                      │
                 │       (interactive transports)               │
                 │                                              │
                 └──────────────────────────────────────────────┘
```

---

## Streaming teardown guarantee

`_consume_stream()` in `agent/core/provider_runner.py` wraps the `async for`
delta loop in `try/finally` so it emits **exactly one**
`StreamChunk(finished=True)` event for every stream, including:

- Streams that raise mid-iteration (caught by the outer retry loop; the
  `finished=True` still fires before the exception propagates)
- Streams that close without a terminal `done=True` delta (logged at WARNING;
  the loop falls back to `END_TURN` or `TOOL_USE` as appropriate)

Without this, SSE transports and TUI consumers that block on the end marker
would hang after a misbehaving provider.

## Related files

| File | Purpose |
|---|---|
| `agent/core/loop.py` | Main `run_loop()` coroutine — only control flow, nothing else |
| `agent/core/provider_runner.py` | Provider request + retry, streaming consumption, error translation |
| `agent/core/loop_helpers.py` | Event emission, usage/budget accounting, `parse_stop`, internal messages |
| `agent/core/guardrails.py` | `LoopGuardrails`, `GuardrailsConfig` |
| `agent/core/config.py` | `AgentConfig` — all config keys |
| `agent/core/state.py` | `AgentState` enum |
| `config/samples/` | Ready-to-use config files per provider |
