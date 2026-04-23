# The Companion Extension

> A small animated creature named **Bit** lives in the upper-right corner of
> Aar's fixed TUI. It's an extension — not a core feature — and it's one of
> the best worked examples of why Aar's extension API is designed the way it
> is.

---

## What it is

The companion extension (`agent/extensions/contrib/companion.py`) wraps the
`CompanionEngine` state machine and surfaces it as:

1. An **event-driven mood** (`happy`, `thinking`, `focused`, `excited`,
   `stressed`, `sleeping`, `level_up`), rendered as an animated kaomoji in
   the `CompanionPanel` widget.
2. A **persistent XP/level system** (5 levels, thresholds at 0/5/15/30/50
   tool calls) that reflects how much useful work the agent has done this
   session.
3. A **`companion_status` tool** the LLM itself can call to introspect the
   companion's current state — useful if a conversation wants to reason
   about its own progress ("am I making progress or spinning?").

---

## How it works

The extension subscribes to four agent lifecycle events and one tool:

| Hook | What it does |
|---|---|
| `session_start` | Instantiates a `CompanionEngine` and calls `bootstrap_from_session(session)` so a resumed session picks up the accumulated level, steps, and error count from prior turns. |
| `tool_call` | Counts as one "step" of productive work — increments `steps`, may trigger a level-up (returns the sparkle animation). |
| `stream_chunk` | Reads the chunk type: reasoning text → `THINKING` mood; answer tokens → `EXCITED` mood. |
| `error` | Increments error counter, sets `STRESSED` mood. |
| `session_end` | Settles the engine into a resting mood (`HAPPY`, `FOCUSED`, or `STRESSED` depending on git health). |

The `CompanionEngine` itself is a **pure, synchronous state machine** with
no Textual/I/O dependencies — it lives in `agent/transports/companion_state.py`
so both the TUI widget and the extension can use it without pulling in
a GUI framework. That split is the secret to why the extension is ~130
lines: all the interesting logic lives in a testable, UI-free engine; the
extension is just a thin wiring layer between agent events and that engine.

### Level curve

```
Level 1: 0 steps   — hatchling                 (◕‿◕)
Level 2: 5 steps   — ears appear                ^_^
Level 3: 15 steps  — wings begin              ٩(◕‿◕｡)۶
Level 4: 30 steps  — glowing                  (￣ー￣)
Level 5: 50 steps  — cosmic                    ✨
```

### Compaction-proof progress

`SessionStore.compact()` truncates old events when a session gets long,
which would normally reset the companion's step count. The extension
survives this because the engine reads a `companion_baseline` watermark
that `compact()` writes into `session.metadata` **before** it prunes:

```python
baseline = session.metadata.get("companion_baseline", {})
steps = baseline.get("steps", 0) + count(ToolCall in session.events)
```

So the companion's lifetime progress is the rolled-over baseline plus the
tool-calls still visible in the session events. No separate persistence
file, no drift.

### Git-aware mood

The TUI also polls `git status --porcelain` every ~3 seconds and forwards
a `GitHealth(dirty_files, untracked_files)` snapshot into the engine via
`apply_git_health()`. A mostly-clean repo keeps Bit `HAPPY`; a few
uncommitted files make him `FOCUSED`; chaos (≥5 dirty/untracked files)
turns him `STRESSED`. The engine only lets git health nudge the **resting**
mood — if Bit is mid-`LEVEL_UP` or `THINKING`, the git probe won't stomp
on that.

---

## Why it's cool

**It is the extension API's "hello, world" that actually does something.**

Every other integration surface in Aar tends to be either:

- *too simple*: a slash command that prints a report, or
- *too tangled with core*: MCP bridging, observability.

The companion sits exactly in the middle. It:

1. **Exercises almost every extension hook** — `session_start`, `tool_call`,
   `stream_chunk`, `error`, `session_end` — so if an extension can do what
   the companion does, it can do anything the API exposes.
2. **Uses closure state cleanly.** The `engine` variable is captured by
   `nonlocal` in `register()`; no globals, no singletons, no DI container.
   Reads like a book.
3. **Registers a tool** so the LLM itself can query it — it's not just a
   UI gimmick, it's a feedback channel the model can introspect.
4. **Survives session compaction** via a cooperative baseline watermark —
   a pattern other "cumulative metric" extensions can copy directly.
5. **Separates logic from rendering.** `CompanionEngine` has zero UI deps,
   so every behavior is unit-testable (`tests/test_companion_state.py`)
   and the same engine drives both the TUI widget and the
   `companion_status` tool. Want to add a web dashboard, an ACP
   side-channel, or a Slack bot? Reuse the engine; write a new thin
   adapter.
6. **It's charming.** A coding agent that levels up as you work with it,
   gets sleepy if you walk away, and looks panicked when your repo is a
   mess — that's a signal channel humans process pre-cognitively. It
   doesn't cost context window, doesn't steal input focus, but it tells
   you at a glance whether the session is healthy.

If you're writing a new Aar extension, skim `agent/extensions/contrib/companion.py`
first. It's 134 lines, most of them comments, and it shows the full
canonical shape: event subscription, closure state, a tool, and an
engine module kept deliberately dumb so the extension can stay thin.

---

## Files

| File | Role |
|---|---|
| `agent/extensions/contrib/companion.py` | The extension itself — `register(api)` + hooks + tool. |
| `agent/transports/companion_state.py` | Pure state engine (`CompanionEngine`, `Mood`, `GitHealth`, `companion_stats_from_session`, `get_git_health`). |
| `agent/transports/tui_widgets/companion.py` | Textual widget that renders the kaomoji/XP bar. |
| `tests/test_companion_state.py` | Engine unit tests (mood transitions, level thresholds, compaction survival). |

---

## Enabling it

The companion extension is a built-in contrib module, not a separately
installed package. It's auto-loaded in the fixed TUI when the companion
panel is enabled in the active theme (`CompanionConfig` on `Theme`), and
its `companion_status` tool becomes available to the LLM as soon as the
extension manager registers it. There is no config flag to flip — presence
in the theme enables the panel, and the extension API wiring takes care
of the rest.
