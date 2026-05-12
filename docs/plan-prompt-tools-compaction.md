# Plan: Skills, LLM-Summarized Compaction, File Tracking, Tool Awareness

Status: **Phase 1 complete**

---

## Phase 1 — Tool-Aware System Prompt

**Goal:** System prompt lists active tools with one-line snippets and contributes conditional
guidelines so the LLM knows what it has *before* parsing tool schemas.

**Status:** ✅ complete

### Steps

- [x] **1a** `agent/tools/schema.py` — add `prompt_snippet: str` and `prompt_guidelines: list[str]` to `ToolSpec`
- [x] **1b** `agent/tools/registry.py` — add `get_prompt_snippets()` and `get_prompt_guidelines()` methods
- [x] **1c** `agent/core/config.py` — `build_system_prompt()` gains `tool_snippets` / `tool_guidelines` params; append sections
- [x] **1d** `agent/core/agent.py` — `_rebuild_system_prompt()` helper; call after builtin + extension registration
- [x] **1e** `agent/tools/builtin/*.py` — populate `prompt_snippet` and `prompt_guidelines` on every built-in tool
- [x] **1f** tests — 22 tests in `tests/test_tool_aware_prompt.py`, all passing

---

## Phase 2 — Skills (Lazy-Load Instructions)

**Goal:** Specialized instructions in `.md` files; only name + description + path in the
system prompt. LLM reads the full file on demand via `read_file`.

**Status:** ⏳ not started

### Steps

- [ ] **2a** `agent/core/skills.py` — `Skill` model, frontmatter parsing
- [ ] **2b** `agent/core/skills.py` — `load_skills()` discovery (global `~/.aar/skills/`, project `.agent/skills/`, extra paths)
- [ ] **2c** `agent/core/skills.py` — `format_skills_for_prompt()` → XML `<available_skills>` block
- [ ] **2d** `agent/core/config.py` — `build_system_prompt()` gains `skills` param; wire into prompt assembly
- [ ] **2e** `agent/core/config.py` — `skills_dirs` and `skills_enabled` fields on `AgentConfig`
- [ ] **2f** `agent/core/agent.py` — call `load_skills()` in `_rebuild_system_prompt()`
- [ ] **2g** tests

---

## Phase 3 — LLM-Summarized Compaction + File Tracking

**Goal:** When context exceeds the window, call the LLM to produce a structured summary
instead of a one-line truncation marker. Track files read/modified across compactions.

**Status:** ⏳ not started

### Steps

- [ ] **3a** `agent/core/compaction.py` — `FileOperations` dataclass, `extract_file_ops()`, `merge_file_ops()`, `format_file_ops()`
- [ ] **3b** `agent/core/compaction.py` — `SUMMARIZATION_PROMPT`, `UPDATE_SUMMARIZATION_PROMPT`, `SUMMARIZATION_SYSTEM_PROMPT`
- [ ] **3c** `agent/core/compaction.py` — `generate_summary()` async function
- [ ] **3d** `agent/core/events.py` — `CompactionSummary` event type
- [ ] **3e** `agent/core/session.py` — `summarize_and_compact()` async function
- [ ] **3f** `agent/core/loop.py` — new `"summarize"` context strategy branch
- [ ] **3g** `agent/core/session.py` — `to_messages()` handles `CompactionSummary` events
- [ ] **3h** `agent/core/config.py` — `compaction_reserve_tokens`, `compaction_keep_recent_tokens` fields
- [ ] **3i** tests

---

## Dependency Graph

Phase 1: Tool-Aware System Prompt  (no dependencies)
Phase 2: Skills                     (depends on Phase 1)
Phase 3: LLM-Summarized Compaction  (independent — parallel with Phase 1)

## File Change Summary

| File | Phase 1 | Phase 2 | Phase 3 |
|---|---|---|---|
| `agent/tools/schema.py` | ✏️ add fields | | |
| `agent/tools/registry.py` | ✏️ add methods | | |
| `agent/tools/builtin/*.py` | ✏️ add snippets | | |
| `agent/core/config.py` | ✏️ prompt params | ✏️ skill config + prompt | ✏️ compaction config |
| `agent/core/agent.py` | ✏️ rebuild prompt | ✏️ load skills | |
| `agent/core/skills.py` | | 🆕 | |
| `agent/core/compaction.py` | | | 🆕 |
| `agent/core/events.py` | | | ✏️ new event type |
| `agent/core/session.py` | | | ✏️ new compact fn |
| `agent/core/loop.py` | | | ✏️ new strategy branch |
