"""Built-in theme definitions: default, claude, bladerunner."""

from __future__ import annotations

from agent.transports.themes.models import (
    BadgeColors,
    FixedLayoutConfig,
    FooterStyle,
    HeaderStyle,
    PanelStyle,
    ScrollbarConfig,
    Theme,
)

# ---------------------------------------------------------------------------
# default — matches the original hardcoded TUI styles exactly
# ---------------------------------------------------------------------------
DEFAULT_THEME = Theme(
    name="default",
    description="Classic Aar palette — green, yellow, cyan, red",
    assistant=PanelStyle(
        title_style="bold green",
        border_style="green",
        padding=(1, 2),
    ),
    tool_call=PanelStyle(
        title_style="bold yellow",
        border_style="yellow",
        padding=(0, 2),
    ),
    tool_result=PanelStyle(
        title_style="bold cyan",
        border_style="cyan",
        padding=(0, 2),
    ),
    tool_error=PanelStyle(
        title_style="bold red",
        border_style="red",
        padding=(0, 2),
    ),
    reasoning=PanelStyle(
        title_style="dim",
        border_style="dim",
        padding=(0, 2),
    ),
    error=PanelStyle(
        title_style="bold red",
        border_style="red",
        padding=(0, 2),
    ),
    welcome=PanelStyle(
        title_style="bold blue",
        border_style="blue",
        padding=(1, 2),
    ),
    prompt_style="bold blue",
    dim_text="dim",
    working_style="dim italic",
    path_highlight="bold blue",
    usage_style="dim",
    badges=BadgeColors(
        read="dim cyan",
        write="yellow",
        execute="red",
        network="blue",
        external="magenta",
    ),
    header=HeaderStyle(
        background="on #1a1a2e",
        text_style="bold white",
        separator_style="dim",
        provider_style="bold cyan",
        tokens_style="dim green",
        session_style="dim",
        state_style="bold yellow",
    ),
    footer=FooterStyle(
        background="on #1a1a2e",
        text_style="bold white",
        separator_style="dim",
        step_style="dim cyan",
        theme_style="dim magenta",
        input_style="bold blue",
    ),
    fixed_layout=FixedLayoutConfig(
        body_background="#0e0e0e",
        input_background="#111118",
        scrollbar=ScrollbarConfig(
            color="#444444",
            color_hover="#666666",
            color_active="#888888",
            background="#1a1a1a",
        ),
    ),
)

# ---------------------------------------------------------------------------
# claude — warm, muted palette inspired by Claude Code
# ---------------------------------------------------------------------------
CLAUDE_THEME = Theme(
    name="claude",
    description="Warm sand and sage — inspired by Claude Code",
    assistant=PanelStyle(
        title_style="bold #d4a574",
        border_style="#d4a574",
        padding=(1, 2),
    ),
    tool_call=PanelStyle(
        title_style="bold #7b8794",
        border_style="#7b8794",
        padding=(0, 2),
    ),
    tool_result=PanelStyle(
        title_style="bold #6b9e78",
        border_style="#6b9e78",
        padding=(0, 2),
    ),
    tool_error=PanelStyle(
        title_style="bold #c75c5c",
        border_style="#c75c5c",
        padding=(0, 2),
    ),
    reasoning=PanelStyle(
        title_style="#5a5a6e",
        border_style="#5a5a6e",
        padding=(0, 2),
    ),
    error=PanelStyle(
        title_style="bold #c75c5c",
        border_style="#c75c5c",
        padding=(0, 2),
    ),
    welcome=PanelStyle(
        title_style="bold #d4a574",
        border_style="#d4a574",
        padding=(1, 2),
    ),
    prompt_style="bold #d4a574",
    dim_text="#7b8794",
    working_style="italic #7b8794",
    path_highlight="bold #6b9e78",
    usage_style="#7b8794",
    badges=BadgeColors(
        read="#7b8794",
        write="#d4a574",
        execute="#c75c5c",
        network="#6b9e78",
        external="#5a5a6e",
    ),
    header=HeaderStyle(
        background="on #2d2a24",
        text_style="bold #d4a574",
        separator_style="#5a5a6e",
        provider_style="bold #6b9e78",
        tokens_style="#7b8794",
        session_style="#7b8794",
        state_style="bold #d4a574",
    ),
    footer=FooterStyle(
        background="on #2d2a24",
        text_style="bold #d4a574",
        separator_style="#5a5a6e",
        step_style="#7b8794",
        theme_style="#5a5a6e",
        input_style="bold #d4a574",
    ),
    fixed_layout=FixedLayoutConfig(
        body_background="#1e1b16",
        input_background="#2d2a24",
        scrollbar=ScrollbarConfig(
            color="#5a5a6e",
            color_hover="#7b8794",
            color_active="#d4a574",
            background="#2d2a24",
        ),
    ),
)

# ---------------------------------------------------------------------------
# bladerunner — cyberpunk neon on dark
# ---------------------------------------------------------------------------
DECKER_THEME = Theme(
    name="decker",
    description="Neon glow — cyberpunk terminal aesthetic",
    assistant=PanelStyle(
        title_style="bold #00fff7",
        border_style="#00fff7",
        padding=(1, 2),
    ),
    tool_call=PanelStyle(
        title_style="bold #ff2d95",
        border_style="#ff2d95",
        padding=(0, 2),
    ),
    tool_result=PanelStyle(
        title_style="bold #39ff14",
        border_style="#39ff14",
        padding=(0, 2),
    ),
    tool_error=PanelStyle(
        title_style="bold #ff0040",
        border_style="#ff0040",
        padding=(0, 2),
    ),
    reasoning=PanelStyle(
        title_style="#9d00ff",
        border_style="#9d00ff",
        padding=(0, 2),
    ),
    error=PanelStyle(
        title_style="bold #ff0040",
        border_style="#ff0040",
        padding=(0, 2),
    ),
    welcome=PanelStyle(
        title_style="bold #00fff7",
        border_style="#ff2d95",
        padding=(1, 2),
    ),
    prompt_style="bold #00fff7",
    dim_text="#ff6e27",
    working_style="italic #9d00ff",
    path_highlight="bold #39ff14",
    usage_style="#ff6e27",
    badges=BadgeColors(
        read="#00fff7",
        write="#ff6e27",
        execute="#ff0040",
        network="#39ff14",
        external="#9d00ff",
    ),
    header=HeaderStyle(
        background="on #0a0a1a",
        text_style="bold #00fff7",
        separator_style="#9d00ff",
        provider_style="bold #39ff14",
        tokens_style="#ff6e27",
        session_style="#ff2d95",
        state_style="bold #00fff7",
    ),
    footer=FooterStyle(
        background="on #0a0a1a",
        text_style="bold #00fff7",
        separator_style="#9d00ff",
        step_style="#ff6e27",
        theme_style="#9d00ff",
        input_style="bold #00fff7",
    ),
    fixed_layout=FixedLayoutConfig(
        body_background="#050510",
        input_background="#0a0a1a",
        scrollbar=ScrollbarConfig(
            color="#9d00ff",
            color_hover="#ff2d95",
            color_active="#00fff7",
            background="#0a0a1a",
        ),
    ),
)

BUILTIN_THEMES: dict[str, Theme] = {t.name: t for t in [DEFAULT_THEME, CLAUDE_THEME, DECKER_THEME]}
