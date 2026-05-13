# Plan: Skills, LLM-Summarized Compaction, File Tracking, Tool Awareness

Status: **All phases complete** (Phase 1 + 2 + 3)

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

**Status:** ✅ complete

### Steps

- [x] **2a** `agent/core/skills.py` — `Skill` model, `parse_frontmatter()`, `strip_frontmatter()`, validation
- [x] **2b** `agent/core/skills.py` — `load_skills()` discovery (global `~/.aar/skills/`, project `.agent/skills/`, extra paths)
- [x] **2c** `agent/core/skills.py` — `format_skills_for_prompt()` → XML `<available_skills>` block
- [x] **2d** `agent/core/config.py` — `build_system_prompt()` + `_collect_layers()` gain `skills_text` param; skills layer inserted after tools, before global rules
- [x] **2e** `agent/core/config.py` — `skills_dirs` and `skills_enabled` fields on `AgentConfig`
- [x] **2f** `agent/core/agent.py` — call `load_skills()` in `_rebuild_system_prompt()`
- [x] **2g** `agent/transports/cli.py` — `aar prompt` command includes skills layer in output and `--layers` view
- [x] **2h** tests — 32 tests in `tests/test_skills.py`, all passing

---

## Phase 3 — LLM-Summarized Compaction + File Tracking

**Goal:** When context exceeds the window, call the LLM to produce a structured summary
instead of a one-line truncation marker. Track files read/modified across compactions.

**Status:** ✅ complete

### Steps

- [x] **3a** `agent/core/compaction/utils.py` — `FileOperations` dataclass, `extract_file_ops_from_message()`, `compute_file_lists()`, `format_file_operations()`, `serialize_conversation()`
- [x] **3b** `agent/core/compaction/compaction.py` — `_INITIAL_PROMPT`, `_UPDATE_PROMPT`, `SUMMARIZATION_SYSTEM_PROMPT`
- [x] **3c** `agent/core/compaction/compaction.py` — `generate_summary()` async, `compact_session()` async
- [x] **3d** `agent/core/compaction/compaction.py` — `estimate_message_tokens()`, `estimate_event_tokens()`, `estimate_context_tokens()`, `should_compact()`, `find_event_cut_point()`, `CompactionResult`
- [x] **3e** `agent/core/session.py` — `events_to_messages()` extracted as standalone function; `Session.apply_compaction()` method
- [x] **3f** `agent/core/loop.py` — new `"summarize"` context strategy branch with fallback to trim
- [x] **3g** `agent/core/config.py` — `CompactionConfig` model (enabled, reserve_tokens, keep_recent_tokens); `compaction` field on `AgentConfig`; `context_strategy` now includes `"summarize"`
- [x] **3h** tests — 46 tests in `tests/test_compaction.py`, all passing

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
| `agent/core/config.py` | ✏️ prompt params | ✏️ skills_text param + skills_dirs/skills_enabled config | ✏️ CompactionConfig + compaction field + "summarize" strategy |
| `agent/core/agent.py` | ✏️ rebuild prompt | ✏️ load skills in _rebuild_system_prompt | |
| `agent/core/skills.py` | | 🆕 Skill model, discovery, formatting | |
| `agent/core/compaction/__init__.py` | | | 🆕 package exports |
| `agent/core/compaction/utils.py` | | | 🆕 file ops, serialization |
| `agent/core/compaction/compaction.py` | | | 🆕 core logic, LLM summary |
| `agent/core/session.py` | | | ✏️ events_to_messages() + apply_compaction() |
| `agent/core/loop.py` | | | ✏️ "summarize" strategy branch |
| `agent/transports/cli.py` | | ✏️ prompt command includes skills | |
| `tests/test_tool_aware_prompt.py` | 🆕 22 tests | | |
| `tests/test_skills.py` | | 🆕 32 tests | |
| `tests/test_compaction.py` | | | 🆕 46 tests |
