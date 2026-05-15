# 🏁 Agent Benchmark Comparison — testplan_v4 Pipeline

## The Task

All 4 agents executed the same `plan_v4.md` (testplan_v4.zip) using Sonnet 4.6 with adaptive thinking: fix a buggy `parser.py` (mutable default arg + loose regex), write tests, implement `main.py`, `verify.py`, and `run.sh`, then run the pipeline to get `V4_ULTIMATE_PIPELINE_SUCCESS`.

---

## Side-by-Side Comparison

| Dimension | 1. ZEDAgent | 2. VSCodeAgent | 3. ClaudeCode (Copilot) | 4. AAR (6th run) |
|---|---|---|---|---|
| **Context used** | 15k / 200k | 19.1k / 200k | 38.2k / 200k | 23.9k total (19.4k in + 4.4k out) |
| **Iterations to pass** | **1** (first try) | **2** (regex fix needed) | **1** (first try) | **1** (first try) |
| **Tool calls** | ~10 (5 reads, 5 writes, 2 terminal) | ~10+ (reads, writes, 2 terminal runs) | ~10 (reads, writes, 2 terminal runs) | **11** (5 reads, 5 writes, 1 bash) |
| **Test count** | **9** | 7 | **4** | 7 |
| **Outcome** | ✅ PASS | ✅ PASS | ✅ PASS | ✅ PASS |

---

## Detailed Analysis

### 1. 🥇 ZEDAgent (Zed Built-in) — **Rank #1**

**Strengths:**
- **Leanest context usage** (15k tokens) — extremely efficient
- **First-try success** — no retry loop needed
- **Best test coverage** (9 tests) with granular edge cases: lowercase, too many letters, too few digits, too many digits, empty, accumulation mode
- Well-structured code: `def main()` with `if __name__` guard, `EXPECTED` constant, docstrings on every test
- Clean ruff-style formatting (double quotes, consistent spacing)

**Weaknesses:**
- None significant — the most polished output of the four

---

### 2. 🥈 AAR 6th Run — **Rank #2**

**Strengths:**
- **First-try success**, clean execution
- **Best observability** — the only agent with a full session report (token counts, cost breakdown, tool call inventory, step count)
- Good test coverage (7 tests) with descriptive failure messages
- Proper code structure (`def main()`, `EXPECTED` constant, preserved docstring)
- **Most efficient tool usage** — exactly 11 tool calls, only **1 terminal invocation** (the others needed 2)
- Detailed `<thinking>` blocks show the agent reasoned deeply about regex word boundaries before writing code

**Weaknesses:**
- Slightly fewer tests than ZEDAgent (7 vs 9 — missing individual too-many-letters / too-few-digits tests, though covered by the combined `test_invalid_tokens_excluded`)
- Higher token usage than ZEDAgent (23.9k vs 15k), partly because of the richer thinking/reasoning

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

### 4. 🏅 ClaudeCode via Copilot VS Code — **Rank #4**

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
| **🥇 1st** | **ZEDAgent** | ⭐⭐⭐⭐⭐ | Leanest context, first-try, most tests (9), cleanest code quality |
| **🥈 2nd** | **AAR (6th run)** | ⭐⭐⭐⭐½ | First-try, best observability, fewest tool calls, strong reasoning — slightly more tokens and fewer tests than ZEDAgent |
| **🥉 3rd** | **VSCodeAgent** | ⭐⭐⭐½ | Decent tests but needed a retry; less structured code |
| **4th** | **ClaudeCode (Copilot)** | ⭐⭐⭐ | Completed the task but used 2.5× the tokens for the weakest output (4 tests, no docstrings, no code structure) |

---

## Key Takeaway

All four agents successfully completed the pipeline — the task itself isn't hard enough to cause failures. The differentiators are **efficiency** (context/tokens consumed), **code quality** (structure, documentation, test coverage), and **reliability** (first-try vs retry). ZEDAgent and AAR stand out for doing it right the first time with well-crafted outputs while using the least resources. The irony of ClaudeCode is that it used the most tokens to produce the least thorough result.

