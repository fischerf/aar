# 🏁 Agent Benchmark Comparison — testplan_v4 Pipeline

(The Agent Benchmark Comparison is evaluated by Opus 4.7)

## The Task

All 4 agents executed the same `plan_v4.md` (testplan_v4.zip) using Sonnet 4.6 with adaptive thinking: fix a buggy `parser.py` (mutable default arg + loose regex), write tests, implement `main.py`, `verify.py`, and `run.sh`, then run the pipeline to get `V4_ULTIMATE_PIPELINE_SUCCESS`.

---

## Side-by-Side Comparison

| Dimension | 1. ZEDAgent | 2. VSCodeAgent | 3. ClaudeCode | 4. AAR |
|---|---|---|---|---|
| **Context / tokens used** | 15k / 200k | 19.1k / 200k | 38.2k / 200k | **37.4k total** (35.5k in + 1.9k out, ~$0.135) |
| **Iterations to pass** | **1** (first try) | **2** (regex fix needed) | **1** (first try) | **1** (first try) |
| **LLM steps** | not reported | not reported | not reported | **5** |
| **Tool calls** | ~10 (5 reads, 5 writes, 2 terminal) | ~10+ (reads, writes, 2 terminal runs) | ~10 (reads, writes, 2 terminal runs) | **9** (2 reads, 1 list_directory, 5 writes, 1 bash) |
| **Test count** | **9** | 7 | 4 | **8** |
| **Outcome** | ✅ PASS | ✅ PASS | ✅ PASS | ✅ PASS |

---

## Detailed Analysis

### 1. 🥇 ZEDAgent (Zed Built-in) — **Rank #1 (tied)**

**Strengths:**
- **Leanest context usage** (15k tokens) — extremely efficient
- **First-try success** — no retry loop needed
- **Highest test count** (9 tests) with granular edge cases: lowercase, too many letters, too few digits, too many digits, empty, accumulation mode
- Well-structured code: `def main()` with `if __name__` guard, `EXPECTED` constant, docstrings on every test
- Clean ruff-style formatting (double quotes, consistent spacing)

**Weaknesses:**
- None significant — the most polished output of the four

---

### 2. 🥇 AAR — **Rank #1 (tied)**

**Strengths:**
- **First-try success** in only **5 LLM steps**
- **Fewest tool calls** of all four agents (9) thanks to aggressive batching — step 1 batches `read_file` + `list_directory`, step 3 writes all 5 source files in one turn, step 4 runs the pipeline. Only **1 terminal invocation** (the others needed 2).
- **Cleanest regex of the four:** `TKN-[A-Z]{3}-\d{4}(?!\d)`. The negative lookahead handles `TKN-LONG-99999` and `TKN-ABC-12345` without depending on word boundaries — the exact case that tripped VSCodeAgent.
- 8 tests, all using `assertEqual` with descriptive failure messages. Covers every category ZEDAgent does **except** explicit accumulation mode:
  - `test_mutable_default_argument_bug` — explicit two-call test
  - Individual edge-case tests for lowercase, too-many-letters, too-few-digits, too-many-digits
  - mixed valid/invalid round-trip, empty, valid-only
- Proper code structure: `def main()` + `__name__` guard, `EXPECTED` constant, docstring preserved on `parser.py`
- **Best observability** — only run with a full session report (token counts, cost, per-tool breakdown, step count, reasoning blocks). Two adaptive-thinking blocks recorded.

**Weaknesses:**
- Slightly fewer tests than ZEDAgent (8 vs 9 — missing the explicit "accumulation mode" test where a pre-existing list is passed in)
- Total billed tokens (~37.4k) is on par with ClaudeCode, ~2.5× ZEDAgent. The richer thinking and full system prompt are the main contributors.
- `run.sh` uses `#!/bin/bash` instead of the more portable `#!/usr/bin/env bash`

---

### 3. 🥉 VSCodeAgent — **Rank #3**

**Strengths:**
- Good test coverage (7 tests) covering all key bug categories
- Created a task decomposition (7 tasks) showing structured planning
- Ultimately correct output

**Weaknesses:**
- **Needed 2 iterations** — initially forgot the `\b` word boundary on the regex, which means the first run failed on `TKN-ABC-12345`. This is the only agent that didn't get it right the first time.
- Uses `assertIn` instead of `assertEqual` for the first test — weaker assertion (doesn't check order or count)
- `main.py` runs at module level (no `def main()` / `__name__` guard) — less structured
- Variable naming inconsistency (`expected` lowercase vs `EXPECTED`)
- Higher context than ZEDAgent (19.1k)

---

### 4. 🏅 ClaudeCode via VS Code — **Rank #4**

**Strengths:**
- First-try success
- Compact, functional code — everything works
- Uses `printf` instead of `echo` in run.sh (technically more portable for special characters)

**Weaknesses:**
- **Highest context usage by far** (38.2k — 2.5× ZEDAgent) for the same task
- **Fewest tests** (only 4) — the minimum needed. Missing: empty text, no tokens, explicit accumulation, individual edge-case tests
- **Stripped the docstring** from `parser.py` entirely — lost documentation
- `main.py` runs at module level (no function wrapper)
- `verify.py` inlines the expected value (no named constant)
- `run.sh` uses `#!/bin/bash` instead of the more portable `#!/usr/bin/env bash`
- Chat log is the least transparent — task outputs are truncated/collapsed, harder to follow the reasoning

---

## Final Rankings

| Rank | Agent | Score | Rationale |
|---|---|---|---|
| **🥇 1 (tied)** | **ZEDAgent** | ⭐⭐⭐⭐⭐ | Leanest context, first-try, most tests (9), cleanest code quality |
| **🥇 1 (tied)** | **AAR** | ⭐⭐⭐⭐⭐ | First-try in 5 steps, fewest tool calls (9), cleanest regex, best observability, near-parity on test count (8). Loses to ZED only on raw token economy. |
| **🥉 3** | **VSCodeAgent** | ⭐⭐⭐½ | Decent tests but needed a retry; less structured code |
| **4** | **ClaudeCode** | ⭐⭐⭐ | Completed the task but used the most tokens for the weakest output (4 tests, no docstrings, no code structure) |

---

## Key Takeaway

All four agents successfully completed the pipeline — the task itself isn't hard enough to cause failures. The differentiators are **efficiency** (tool calls, steps, tokens), **code quality** (structure, documentation, test coverage), and **reliability** (first-try vs retry).

ZEDAgent and AAR are essentially tied at the top: ZED edges ahead on raw token economy and one extra test, while AAR edges ahead on tool-call efficiency, regex correctness, and observability. Both produce well-structured artefacts on the first try.

The irony of ClaudeCode is that it used the most tokens to produce the least thorough result.
