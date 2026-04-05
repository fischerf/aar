"""Built-in theme definitions: default, claude, bladerunner."""

from __future__ import annotations

from agent.transports.themes.models import BadgeColors, PanelStyle, Theme

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
)

# ---------------------------------------------------------------------------
# bladerunner — cyberpunk neon on dark
# ---------------------------------------------------------------------------
BLADERUNNER_THEME = Theme(
    name="bladerunner",
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
)

BUILTIN_THEMES: dict[str, Theme] = {
    t.name: t for t in [DEFAULT_THEME, CLAUDE_THEME, BLADERUNNER_THEME]
}
