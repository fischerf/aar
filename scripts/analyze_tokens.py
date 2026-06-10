#!/usr/bin/env python3
"""Token breakdown analyser for Aar session JSONL files.

Usage:
    python scripts/analyze_tokens.py [path/to/session.jsonl]
    python scripts/analyze_tokens.py              # auto-finds latest session

What it shows
-------------
- Per-step actual input/output/cache tokens from ProviderMeta events
- Estimated size of each message component at every step boundary
- Context growth delta between steps with component attribution
- Top token waste contributors ranked by cost
- Optionally uses Anthropic count_tokens API for exact system/tools measurement
  (pass --exact; needs ANTHROPIC_API_KEY and the default provider to be Anthropic)

Interpretation guide
--------------------
The agent re-sends the *entire* conversation every step.  Input tokens for
step N include:
    [system_prompt] + [tool_schemas] + [user_msg] + [all prior turns]

So the input grows with each step.  The delta between steps equals whatever
new content was added in the previous turn (tool calls + tool results +
assistant text), plus the static system/tool overhead re-sent again.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CHAR_PER_TOKEN = 4  # rough English heuristic


def est(text: Any) -> int:
    """Estimate token count from raw text or any JSON-serialisable value."""
    return len(str(text)) // CHAR_PER_TOKEN


def bar(ratio: float, width: int = 30, fill: str = "█", empty: str = "░") -> str:
    filled = round(ratio * width)
    return fill * filled + empty * (width - filled)


def pct(part: int, total: int) -> str:
    if total == 0:
        return "  n/a"
    return f"{part / total * 100:5.1f}%"


# ---------------------------------------------------------------------------
# JSONL loading
# ---------------------------------------------------------------------------


def load_jsonl(path: Path) -> tuple[dict, list[dict]]:
    """Return (meta_row, list_of_events) from a session JSONL file."""
    meta: dict = {}
    events: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("_meta"):
                meta = obj
            else:
                events.append(obj)
    return meta, events


def auto_find_jsonl() -> Path | None:
    """Search common locations for the most recently modified session JSONL."""
    candidates = list(Path(".").rglob(".agent/sessions/*.jsonl"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


# ---------------------------------------------------------------------------
# Step reconstruction
# ---------------------------------------------------------------------------

SKIP_IN_CONTEXT = {"stream_chunk", "context_window", "reasoning"}


def split_into_steps(events: list[dict]) -> list[dict]:
    """Split the flat event list into per-step dicts.

    Each step dict has:
        meta      — the provider_meta event (or None for the final tail)
        events    — events that belong to that step (excludes stream/reasoning)
    """
    steps: list[dict] = []
    bucket: list[dict] = []
    for e in events:
        t = e.get("type", "")
        if t == "provider_meta":
            steps.append({"meta": e, "events": bucket})
            bucket = []
        elif t not in SKIP_IN_CONTEXT:
            bucket.append(e)
    if bucket:
        steps.append({"meta": None, "events": bucket})
    return steps


def estimate_event_tokens(e: dict) -> int:
    """Estimate the token cost of a single event as it appears in the context."""
    t = e.get("type", "")
    if t == "user_message":
        return est(e.get("content", ""))
    if t == "assistant_message":
        return est(e.get("content", ""))
    if t == "tool_call":
        # tool_use block: name + id + arguments JSON
        return est(e.get("tool_name", "")) + est(json.dumps(e.get("arguments", {})))
    if t == "tool_result":
        # tool_result block: output text
        return est(e.get("output", ""))
    return 0


# ---------------------------------------------------------------------------
# Optional: Anthropic count_tokens for exact system+tools measurement
# ---------------------------------------------------------------------------


def count_tokens_exact(model: str) -> dict[str, int] | None:
    """Use Anthropic count_tokens endpoint to measure static components.

    Returns {"system_prompt": N, "tool_schemas": N} or None on failure.
    Requires:  pip install anthropic  and  ANTHROPIC_API_KEY set.
    """
    try:
        import anthropic  # type: ignore

        from agent.core.config import load_config
        from agent.tools.registry import build_default_registry
    except ImportError:
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    try:
        cfg = load_config()
        system_prompt = cfg.system_prompt

        registry = build_default_registry(cfg)
        tool_schemas = registry.to_provider_schemas() or []

        client = anthropic.Anthropic(api_key=api_key)

        # 1. system prompt only
        r_sys = client.messages.count_tokens(
            model=model,
            system=system_prompt,
            messages=[{"role": "user", "content": "hi"}],
        )
        # 2. system prompt + tools
        r_tools = client.messages.count_tokens(
            model=model,
            system=system_prompt,
            tools=tool_schemas,  # type: ignore[arg-type]
            messages=[{"role": "user", "content": "hi"}],
        )
        # Minimal baseline (no system, no tools)
        r_base = client.messages.count_tokens(
            model=model,
            messages=[{"role": "user", "content": "hi"}],
        )

        base = r_base.input_tokens
        sys_only = r_sys.input_tokens - base
        tools_only = r_tools.input_tokens - r_sys.input_tokens

        return {"system_prompt": sys_only, "tool_schemas": tools_only, "base_msg": base}
    except Exception as exc:
        print(f"  [count_tokens failed: {exc}]", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

W = 72  # total report width


def hline(char: str = "─") -> str:
    return char * W


def section(title: str) -> str:
    pad = W - len(title) - 4
    return f"┌─ {title} {'─' * pad}┐"


def row(*cols: str, widths: list[int]) -> str:
    cells = [c.ljust(w)[:w] for c, w in zip(cols, widths)]
    return "│ " + " │ ".join(cells) + " │"


def print_report(
    meta: dict,
    steps: list[dict],
    exact: dict[str, int] | None = None,
) -> None:
    total_input = meta.get("total_input_tokens", 0)
    total_output = meta.get("total_output_tokens", 0)
    total_cost = meta.get("total_cost", 0.0)
    model = ""
    cache_write_total = 0
    cache_read_total = 0

    step_data: list[dict] = []
    for i, step in enumerate(steps):
        m = step["meta"]
        if m is None:
            continue
        u = m.get("usage", {})
        inp = u.get("input_tokens", 0)
        out = u.get("output_tokens", 0)
        cw = u.get("cache_write_tokens", u.get("cache_creation_input_tokens", 0))
        cr = u.get("cache_read_tokens", u.get("cache_read_input_tokens", 0))
        dur = m.get("duration_ms", 0)
        model = m.get("model", model)
        cache_write_total += cw
        cache_read_total += cr
        step_data.append(
            {
                "step": i + 1,
                "inp": inp,
                "out": out,
                "cw": cw,
                "cr": cr,
                "dur": dur,
                "request_id": m.get("request_id", ""),
                "events": step["events"],
            }
        )

    caching_active = cache_write_total > 0 or cache_read_total > 0

    # Detect Vertex AI proxy from request_id prefix ("msg_vrtx_…").
    # Vertex AI silently drops cache_control markers — caching is a no-op there.
    request_ids = [sd.get("request_id", "") for sd in step_data]
    via_vertex = any("vrtx" in rid.lower() for rid in request_ids)

    print()
    print("=" * W)
    print(f"  AAR TOKEN BREAKDOWN ANALYSIS")
    print("=" * W)
    print(f"  Session : {meta.get('session_id', '?')}")
    print(f"  Model   : {model}")
    print(f"  Steps   : {meta.get('step_count', len(step_data))}")
    print(
        f"  Total   : {total_input:,} in / {total_output:,} out = {total_input + total_output:,} total"
    )
    print(f"  Cost    : ${total_cost:.4f}")
    if caching_active:
        caching_status = f"ACTIVE  write={cache_write_total:,}  read={cache_read_total:,}"
    elif via_vertex:
        caching_status = (
            "CONFIGURED but INACTIVE — Vertex AI proxy ignores cache_control markers\n"
            "             Request IDs contain 'vrtx': caching only works on api.anthropic.com"
        )
    else:
        caching_status = 'INACTIVE — set "extra": {"prompt_caching": true} in provider config'
    print(f"  Cache   : {caching_status}")
    print()

    # ── Section 1: per-step token table ─────────────────────────────────────
    print(section("Per-step token usage"))
    W5 = [5, 7, 7, 7, 7, 8, 6, 18]
    print(
        row("Step", "Input", "Output", "Δ Input", "Cch-W", "Cch-R", "ms", "Tool calls", widths=W5)
    )
    print("├" + "─" * (W - 2) + "┤")
    prev_inp = 0
    for sd in step_data:
        tc_summary = _tc_summary(sd["events"])
        delta = sd["inp"] - prev_inp
        prev_inp = sd["inp"]
        print(
            row(
                str(sd["step"]),
                f"{sd['inp']:,}",
                f"{sd['out']:,}",
                f"{delta:+,}",
                str(sd["cw"]) if sd["cw"] else "—",
                str(sd["cr"]) if sd["cr"] else "—",
                f"{sd['dur']:.0f}",
                tc_summary,
                widths=W5,
            )
        )
    print("└" + "─" * (W - 2) + "┘")
    print()

    # ── Section 2: Step 1 context composition ───────────────────────────────
    s1 = step_data[0]
    user_msg_est = 0
    for e in s1["events"]:
        if e.get("type") == "user_message":
            user_msg_est = est(e.get("content", ""))
            break

    static_est = s1["inp"] - user_msg_est - 20  # 20 ≈ framing
    if exact:
        sys_est = exact["system_prompt"]
        tools_est = exact["tool_schemas"]
    else:
        # Heuristic split: system ~70%, tools ~30% of static
        sys_est = int(static_est * 0.70)
        tools_est = static_est - sys_est

    print(section(f"Step 1 context composition  (actual input = {s1['inp']:,} tokens)"))
    components = [
        ("System prompt", sys_est, "re-sent every step" + (" (estimated)" if not exact else "")),
        ("Tool schemas", tools_est, f"re-sent every step" + (" (estimated)" if not exact else "")),
        ("User message (plan text)", user_msg_est, "static per session"),
        ("Framing / JSON overhead", s1["inp"] - sys_est - tools_est - user_msg_est, ""),
    ]
    cw_labels = [24, 7, 26, 10]
    for label, tokens, note in components:
        b = bar(tokens / s1["inp"] if s1["inp"] else 0, width=20)
        print(f"│  {label:<28} {tokens:>6,} tok  {b}  {pct(tokens, s1['inp'])}  {note}")
    print("└" + "─" * (W - 2) + "┘")
    if not exact:
        print("  ↳ tip: run with --exact (needs ANTHROPIC_API_KEY) for precise system/tools split")
    print()

    # ── Section 3: context growth per step ──────────────────────────────────
    print(section("Context growth between steps"))
    prev = 0
    for sd in step_data:
        delta = sd["inp"] - prev
        prev = sd["inp"]
        if sd["step"] == 1:
            continue
        # The events that caused this delta live in the CURRENT step's bucket
        # (events emitted between the *previous* API response and *this* API call).
        cur_step = step_data[sd["step"] - 1]
        new_events = cur_step["events"]
        tc_tok = sum(estimate_event_tokens(e) for e in new_events if e["type"] == "tool_call")
        tr_tok = sum(estimate_event_tokens(e) for e in new_events if e["type"] == "tool_result")
        am_tok = sum(
            estimate_event_tokens(e) for e in new_events if e["type"] == "assistant_message"
        )
        content_est = tc_tok + tr_tok + am_tok
        overhead_est = delta - content_est
        print(f"│  Step {sd['step'] - 1} → {sd['step']}   Δ = {delta:+,} tokens")
        if tc_tok:
            print(f"│    tool_call args    {tc_tok:>6,} ~{pct(tc_tok, delta)}")
        if tr_tok:
            print(f"│    tool_results      {tr_tok:>6,} ~{pct(tr_tok, delta)}")
        if am_tok:
            print(f"│    assistant text    {am_tok:>6,} ~{pct(am_tok, delta)}")
        print(f"│    JSON framing      {overhead_est:>6,} ~{pct(overhead_est, delta)}")
    print("└" + "─" * (W - 2) + "┘")
    print()

    # ── Section 4: waste analysis ────────────────────────────────────────────
    n_steps = len(step_data)

    # Waste 1: static overhead re-sent every extra step
    static_resend = static_est * (n_steps - 1)
    # If caching were active, cache_read costs ~10% of input price → 90% savings
    cache_savings = int(static_est * (n_steps - 1) * 0.90)

    # Waste 2: plan read-back. Tool results land in step_data[1]["events"]
    # (events emitted after step 1's API response, before step 2's API call).
    first_step_tr = (
        [
            e
            for e in step_data[1]["events"]
            if e.get("type") == "tool_result" and e.get("tool_name") == "read_file"
        ]
        if len(step_data) > 1
        else []
    )
    plan_readback = max(
        (est(e.get("output", "")) for e in first_step_tr if len(e.get("output", "")) > 500),
        default=0,
    )
    plan_readback_waste = plan_readback * (n_steps - 1)

    # Waste 3: line-number prefix overhead in read_file output
    linenum_overhead = sum(e.get("output", "").count("\n") * 2 for e in first_step_tr)
    linenum_tok = linenum_overhead // CHAR_PER_TOKEN

    # Waste 4: output token overhead from reasoning blocks (generated but never in context)
    reasoning_tokens = 0
    for step in steps:
        for e in step["events"]:
            if e.get("type") == "reasoning":
                reasoning_tokens += est(e.get("content", ""))

    print(section("Top waste contributors"))
    waste_items = [
        (
            "No prompt caching: system+tools re-sent every step",
            static_resend,
            f"~{cache_savings:,} tok saveable with prompt_caching enabled",
        ),
        (
            "Plan file read-back duplication",
            plan_readback_waste,
            f"plan in user_msg + read_file result ~{plan_readback:,} tok × {n_steps - 1} steps",
        ),
        (
            "Line-number prefixes in read_file output",
            linenum_tok * (n_steps - 1),
            '"     1\\t" added to every line, carried through remaining steps',
        ),
        (
            "Reasoning blocks (output tokens, never re-sent)",
            reasoning_tokens,
            "generated & discarded — pure output cost with no context value",
        ),
    ]
    waste_items_sorted = sorted(
        [(label, tok, note) for label, tok, note in waste_items if tok > 0],
        key=lambda x: x[1],
        reverse=True,
    )
    seen = set()
    rank = 1
    for label, tok, note in waste_items_sorted:
        if label in seen:
            continue
        seen.add(label)
        b = bar(tok / total_input if total_input else 0, width=16)
        print(f"│  #{rank}  {label}")
        print(f"│       ≈ {tok:,} tokens  {b}  {pct(tok, total_input)} of total input")
        if note:
            print(f"│       → {note}")
        rank += 1
    print("└" + "─" * (W - 2) + "┘")
    print()

    # ── Section 5: quick wins ────────────────────────────────────────────────
    print(section("Quick wins"))
    wins = []
    if not caching_active:
        savings_pct = cache_savings / total_input * 100 if total_input else 0
        if via_vertex:
            wins.append(
                f"  ✦  Prompt caching IS configured but Vertex AI proxy drops the markers.\n"
                f"       ~{cache_savings:,} tokens (~{savings_pct:.0f}% of input) could be saved at 10% price.\n"
            )
        else:
            wins.append(
                f"  ✦  Enable prompt caching  →  save ~{cache_savings:,} tokens "
                f"(~{savings_pct:.0f}% of input) at ~10% cache_read price\n"
                f'       Set  "extra": {{"prompt_caching": true}}  in your provider config.'
            )
    if plan_readback > 0:
        wins.append(
            f"  ✦  Skip reading plan file if already in user message  →  save "
            f"~{plan_readback_waste:,} tokens\n"
            f"       The plan was inlined in the prompt AND read back via read_file."
        )
    if linenum_tok > 10:
        wins.append(
            f"  ✦  Strip line-number prefixes from read_file output  →  save "
            f"~{linenum_tok * (n_steps - 1):,} tokens\n"
            f"       Or add a tool param strip_line_numbers=true."
        )
    if reasoning_tokens > 0:
        wins.append(
            f"  ✦  Reasoning blocks cost {reasoning_tokens:,} output tokens but are never re-sent.\n"
            f'       Consider  thinking: {{"type": "disabled"}}  for mechanical tasks.'
        )
    for w in wins:
        print(w)
    if not wins:
        print("  ✦  No obvious quick wins — session looks well optimised.")
    print()
    print("=" * W)
    print()


# ---------------------------------------------------------------------------
# Tool-call summary helper
# ---------------------------------------------------------------------------


def _tc_summary(events: list[dict]) -> str:
    counts: dict[str, int] = {}
    for e in events:
        if e.get("type") == "tool_call":
            n = e.get("tool_name", "?")
            counts[n] = counts.get(n, 0) + 1
    if not counts:
        return "—"
    parts = [f"{v}× {k}" for k, v in sorted(counts.items(), key=lambda x: -x[1])]
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "jsonl",
        nargs="?",
        help="Path to a session .jsonl file. Omit to auto-find the latest.",
    )
    parser.add_argument(
        "--exact",
        action="store_true",
        help="Use Anthropic count_tokens API for exact system+tools size (needs ANTHROPIC_API_KEY).",
    )
    args = parser.parse_args()

    if args.jsonl:
        path = Path(args.jsonl)
    else:
        path = auto_find_jsonl()
        if path is None:
            print("No session JSONL found. Pass a path explicitly.", file=sys.stderr)
            sys.exit(1)
        print(f"Auto-selected: {path}")

    meta, events = load_jsonl(path)
    steps = split_into_steps(events)

    exact: dict[str, int] | None = None
    if args.exact:
        model = ""
        for e in events:
            if e.get("type") == "provider_meta":
                model = e.get("model", "")
                break
        print(f"  Measuring exact system/tools size via count_tokens (model={model})…")
        exact = count_tokens_exact(model)
        if exact:
            print(
                f"  → system_prompt={exact['system_prompt']:,}  tool_schemas={exact['tool_schemas']:,}"
            )
        else:
            print("  → count_tokens unavailable, falling back to estimates.")

    print_report(meta, steps, exact=exact)


if __name__ == "__main__":
    main()
