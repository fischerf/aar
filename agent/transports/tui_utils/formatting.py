"""Formatting helpers shared by all TUI transports."""

from __future__ import annotations

from typing import Any

from agent.transports.themes.builtin import DEFAULT_THEME
from agent.transports.themes.models import Theme


def _side_effect_badge(side_effects: list[str], theme: Theme) -> str:
    badges = theme.badges
    mapping = {
        "read": f"[{badges.read}][read][/]",
        "write": f"[{badges.write}][write][/]",
        "execute": f"[{badges.execute}][exec][/]",
        "network": f"[{badges.network}][net][/]",
        "external": f"[{badges.external}][ext][/]",
    }
    parts = [mapping[e] for e in side_effects if e in mapping]
    return " ".join(parts)


def _looks_like_path(s: str) -> bool:
    return len(s) < 120 and ("/" in s or "\\" in s)


_APPROVAL_MAX_VALUE_LEN = 120

_LARGE_VALUE_KEYS = {"content", "old_string", "new_string", "file_text", "new_content"}


def _format_args(
    arguments: dict[str, Any], verbose: bool = False, theme: Theme | None = None
) -> str:
    t = theme or DEFAULT_THEME
    lines = []
    for k, v in arguments.items():
        val = str(v)
        if len(val) > 300:
            val = val[:300] + "..."
        if verbose and _looks_like_path(val):
            lines.append(f"[bold]{k}:[/] [{t.path_highlight}]{val}[/]")
        else:
            lines.append(f"[bold]{k}:[/] {val}")
    return "\n".join(lines) if lines else "(no arguments)"


def _format_approval_args(arguments: dict[str, Any], max_len: int = _APPROVAL_MAX_VALUE_LEN) -> str:
    """Format tool arguments for the approval prompt.

    Values are aggressively truncated because the full tool call is already
    displayed above the approval widget.  For known large-content keys the
    value is replaced with a byte-length note; other values are truncated to
    *max_len* characters.
    """
    lines: list[str] = []
    for k, v in arguments.items():
        val = str(v)
        if k in _LARGE_VALUE_KEYS and len(val) > max_len:
            n_lines = val.count("\n") + 1
            lines.append(f"  {k}: ({len(val)} chars, {n_lines} lines — shown above)")
        elif len(val) > max_len:
            lines.append(f"  {k}: {val[:max_len]}…")
        else:
            lines.append(f"  {k}: {val}")
    return "\n".join(lines) if lines else "  (no arguments)"


def format_token_display(
    input_tokens: int,
    output_tokens: int,
    cost: float = 0.0,
    show_cost: bool = True,
) -> str:
    """Format token counts and optional cost for TUI display.

    Returns e.g. "150in / 80out ($0.0032)" or "150in / 80out" if no cost.
    """
    parts = f"{input_tokens}in / {output_tokens}out"
    if show_cost and cost > 0:
        if cost < 0.01:
            parts += f" (${cost:.4f})"
        else:
            parts += f" (${cost:.2f})"
    return parts


def is_over_warning_threshold(
    current: float,
    limit: float,
    threshold: float = 0.8,
) -> bool:
    """Check whether *current* has passed *threshold* fraction of *limit*.

    Returns False when *limit* is zero (disabled).
    """
    if limit <= 0:
        return False
    return current >= limit * threshold
