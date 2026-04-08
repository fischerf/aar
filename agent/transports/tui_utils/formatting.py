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
