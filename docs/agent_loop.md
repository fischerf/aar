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
│  │  context_window > 0 & strategy="compact"                  │  │
│  │    → compact_to_token_budget(messages, context_window)    │  │
│  │      keeps first msg + last N msgs; inserts marker        │  │
│  │  context_window > 0 & strategy="summarize"                │  │
│  │    → LLM-based compaction (CompactionConfig.enabled)      │  │
│  │      summarises older messages into a checkpoint          │  │
│  │  strategy="none" → no automatic context management        │  │
│  └───────────────────────────────────────────────────────────┘  │
│                           │                                     │
│                           ▼                                     │
│  ┌─ Provider request (with retries) ─────────────────────────┐  │
│  │  • streaming or complete()                                │  │
│  │  • exponential back-off with jitter on recoverable errors │  │
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

### 4. Premature end-turn recovery

| Config key | Default | Effect |
|---|---|---|
| `max_premature_end_recoveries` | `2` | How many times the loop re-prompts after an empty `end_turn` while recent tool outputs show failures |

**Behaviour:**

```
Model stops with stop_reason = "end_turn" AND response content is empty
  └─ recent tool results contain error markers?
       ("FAIL:", "Error", "Traceback", "AssertionError", "Exit code:", …)
       YES → recovery_count < max_premature_end_recoveries?
              YES → inject internal user message + loop again
                    "You stopped without completing the task. The most
                     recent tool outputs show errors or test failures
                     that still need to be resolved. … try a different
                     approach. Do NOT repeat the same fix — try
                     something materially different."
              NO  → treat as normal END_TURN, exit loop
       NO  → normal END_TURN, exit loop
```

Detection is conservative: the model must produce an *empty* assistant
message and the last ten events must contain at least one failing
`ToolResult`. This catches the "silent give-up" failure mode without
triggering on a legitimate "task complete" finish.

### 5. Bash → `acp_terminal` pivot hint

| Config key | Default | Effect |
|---|---|---|
| `bash_failure_threshold` | `2` | Consecutive `bash` failures (`command not found`, `ImportError`, `ModuleNotFoundError`, `No such file or directory`, `SyntaxError`) before a one-shot hint is injected |

**Behaviour:**

```
Each tool result batch:
  acp_terminal registered AND not already hinted?
    bash call failed with one of the known patterns?
      YES → consecutive_bash_failures += 1
      NO  → reset (on any successful bash call)

    consecutive_bash_failures ≥ bash_failure_threshold?
      YES → inject one-shot internal user message:
            "[System hint: The bash tool runs inside WSL which has a
             different environment from the Windows host. The
             acp_terminal tool is available and runs commands in the
             native Windows host environment …]"
            mark bash_pivot_hinted = True
```

Fires at most **once per session** so the model is not nagged. Only
activates when the ACP transport has registered the `acp_terminal`
tool — invisible in non-ACP runs.

### 6. Read-only loop nudge

| Config key | Default | Effect |
|---|---|---|
| `read_only_loop_threshold` | `8` | Consecutive read-only tool calls before nudging the model to act |

**Behaviour:**

```
Each step with tool calls:
  all tool_calls in {read_file, grep, find_files, list_directory,
                     find_projects, read_issue, list_my_issues,
                     whoami}?
    YES → consecutive_read_only_steps += 1
    NO  → reset to 0

  consecutive_read_only_steps ≥ read_only_loop_threshold
    AND not already nudged?
      YES → inject one-shot internal user message:
            "[System] You have spent many steps reading without
             taking action. Summarize what you've learned so far,
             formulate a concrete plan, and begin implementation. Do
             not read more files unless absolutely necessary for the
             next step."
            mark read_only_nudge_given = True
```

Fires at most **once per session**. The threshold default of `8` is
tuned for codebase exploration — most legitimate research turns are
shorter; turns that exceed `8` usually indicate the model is stuck in
a read-loop and needs to commit to a plan.

---


## Config section

```json
"guardrails": {
  "max_tokens_recoveries":        2,    // auto-retry truncated responses (0 = off)
  "max_repeated_tool_steps":      3,    // consecutive identical tool calls before abort
  "max_premature_end_recoveries": 2,    // re-prompt on empty end_turn with failing tool output
  "reserve_tokens":               512,  // near_budget() token headroom
  "reserve_cost_fraction":        0.1,  // near_budget() cost headroom (fraction of cost_limit)
  "bash_failure_threshold":       2,    // consecutive bash failures before acp_terminal hint (ACP only)
  "read_only_loop_threshold":     8     // consecutive read-only steps before read-loop nudge
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
