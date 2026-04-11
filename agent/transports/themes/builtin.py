"""Built-in theme definitions: default (bernstein), contrast, decker, sleek."""

from __future__ import annotations

from agent.transports.themes.models import (
    BadgeColors,
    FixedLayoutConfig,
    FixedLayoutRegion,
    FooterStyle,
    HeaderStyle,
    InputFieldStyle,
    PanelStyle,
    ScrollbarConfig,
    SeparatorStyle,
    Theme,
    ThinkingPanelConfig,
)

_DEFAULT_SCROLL_SPEED = 3

# ---------------------------------------------------------------------------
# default — slim, warm amber palette (bernstein)
# ---------------------------------------------------------------------------
BERNSTEIN_THEME = Theme(
    name="default",
    description="A slim, modern theme inspired by the warm, sophisticated Bernstein amber palette.",
    assistant=PanelStyle(
        title_style="bold #ffb30f",
        border_style="bold #ffb30f",
        padding=(1, 2),
    ),
    tool_call=PanelStyle(
        title_style="bold #ff2900",
        border_style="#ff2900",
        padding=(0, 2),
    ),
    tool_result=PanelStyle(
        title_style="bold #ffb30f",
        border_style="#ffb30f",
        padding=(0, 2),
    ),
    tool_error=PanelStyle(
        title_style="bold #d12200",
        border_style="#d12200",
        padding=(0, 2),
    ),
    reasoning=PanelStyle(
        title_style="dim #ffb30f",
        border_style="dim #ffb30f",
        padding=(0, 2),
    ),
    error=PanelStyle(
        title_style="bold #d12200",
        border_style="#d12200",
        padding=(0, 2),
    ),
    welcome=PanelStyle(
        title_style="bold #ffb30f",
        border_style="bold #ffb30f",
        padding=(1, 2),
    ),
    prompt_style="bold #ffb30f",
    dim_text="dim #888888",
    working_style="dim #ffb30f",
    path_highlight="bold #ffb30f",
    usage_style="dim #cccccc",
    badges=BadgeColors(
        read="dim #ffb30f",
        write="yellow",
        execute="red",
        network="blue",
        external="magenta",
    ),
    header=HeaderStyle(
        background="on #1a1a2e",
        text_style="bold #ffffff",
        separator_style="dim #444444",
        separator=SeparatorStyle(character="─", style="dim #444444"),
        provider_style="bold #ffb30f",
        tokens_style="dim #cccccc",
        session_style="dim #888888",
        state_style="bold #ffb30f",
    ),
    footer=FooterStyle(
        background="on #1a1a2e",
        text_style="bold #ffffff",
        separator_style="dim #444444",
        separator=SeparatorStyle(character="─", style="dim #444444"),
        step_style="dim #ffb30f",
        theme_style="dim #cccccc",
        input_style="bold #ffb30f",
    ),
    fixed_layout=FixedLayoutConfig(
        regions=[
            FixedLayoutRegion(name="header", size=1),
            FixedLayoutRegion(name="body"),
            FixedLayoutRegion(name="input", size=3),
            FixedLayoutRegion(name="footer", size=1),
        ],
        body_background="#121218",
        input_background="#101014",
        input_field=InputFieldStyle(
            border_color="#444444",
            border_color_focus="#ffb30f",
            cursor_background="#ffb30f",
            cursor_foreground="#121218",
            text_color="#ffb30f",
        ),
        selected_block_style="on #302a1c",
        scrollbar=ScrollbarConfig(
            color="#3a3a3f",
            color_hover="#555560",
            color_active="#777780",
            background="#1a1a1f",
            background_hover="#222227",
            background_active="#222227",
            size=1,
            scroll_speed=5,
        ),
        thinking_panel=ThinkingPanelConfig(
            enabled=True,
            side="right",
            width=40,
            background="#0e0e10",
            border_style="#2a2a1a",
            text_style="italic #886600",
            title_style="dim #554400",
            scrollbar=ScrollbarConfig(
                color="#2a2a1f",
                color_hover="#3a3a2f",
                color_active="#555540",
                background="#0e0e10",
                scroll_speed=5,
            ),
        ),
    ),
)

DEFAULT_THEME = BERNSTEIN_THEME

# ---------------------------------------------------------------------------
# contrast — matches the original hardcoded TUI styles exactly
# ---------------------------------------------------------------------------
CLASSIC_THEME = Theme(
    name="contrast",
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
        separator=SeparatorStyle(style="dim"),
        provider_style="bold cyan",
        tokens_style="dim green",
        session_style="dim",
        state_style="bold yellow",
    ),
    footer=FooterStyle(
        background="on #1a1a2e",
        text_style="bold white",
        separator_style="dim",
        separator=SeparatorStyle(style="dim"),
        step_style="dim cyan",
        theme_style="dim magenta",
        input_style="bold blue",
    ),
    fixed_layout=FixedLayoutConfig(
        body_background="#0e0e0e",
        input_background="#111118",
        input_field=InputFieldStyle(
            border_color="#444444",
            border_color_focus="#888888",
            cursor_background="#cccccc",
            cursor_foreground="#000000",
            text_color="#ffffff",
        ),
        selected_block_style="on #1a2a3a",
        scrollbar=ScrollbarConfig(
            color="#444444",
            color_hover="#666666",
            color_active="#888888",
            background="#1a1a1a",
            scroll_speed=_DEFAULT_SCROLL_SPEED,
        ),
        thinking_panel=ThinkingPanelConfig(
            enabled=True,
            side="right",
            width=40,
            background="#090909",
            border_style="#1a2a1a",
            text_style="italic dim green",
            title_style="dim green",
            scrollbar=ScrollbarConfig(
                color="#1a3a1a",
                color_hover="#2a4a2a",
                color_active="#3a5a3a",
                background="#090909",
            ),
        ),
    ),
)

# ---------------------------------------------------------------------------
# decker — cyberpunk neon on dark
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
        separator=SeparatorStyle(style="#9d00ff"),
        provider_style="bold #39ff14",
        tokens_style="#ff6e27",
        session_style="#ff2d95",
        state_style="bold #00fff7",
    ),
    footer=FooterStyle(
        background="on #0a0a1a",
        text_style="bold #00fff7",
        separator_style="#9d00ff",
        separator=SeparatorStyle(style="#9d00ff"),
        step_style="#ff6e27",
        theme_style="#9d00ff",
        input_style="bold #00fff7",
    ),
    fixed_layout=FixedLayoutConfig(
        body_background="#050510",
        input_background="#0a0a1a",
        input_field=InputFieldStyle(
            border_color="#9d00ff",
            border_color_focus="#00fff7",
            cursor_background="#00fff7",
            cursor_foreground="#050510",
            text_color="#00fff7",
        ),
        selected_block_style="on #1a0a2e",
        scrollbar=ScrollbarConfig(
            color="#9d00ff",
            color_hover="#ff2d95",
            color_active="#00fff7",
            background="#0a0a1a",
            scroll_speed=_DEFAULT_SCROLL_SPEED,
        ),
        thinking_panel=ThinkingPanelConfig(
            enabled=True,
            side="right",
            width=42,
            background="#050510",
            border_style="#3d0066",
            text_style="italic #9d00ff",
            title_style="#6600aa",
            scrollbar=ScrollbarConfig(
                color="#3d0066",
                color_hover="#6600aa",
                color_active="#9d00ff",
                background="#050510",
            ),
        ),
    ),
)

# ---------------------------------------------------------------------------
# sleek — tight spacing, minimal chrome, modern dark palette
# ---------------------------------------------------------------------------
SLEEK_THEME = Theme(
    name="sleek",
    description="Tight spacing, minimal chrome — compact and modern",
    assistant=PanelStyle(
        title_style="bold #a0d2db",
        border_style="#a0d2db",
        padding=(0, 1),
    ),
    tool_call=PanelStyle(
        title_style="bold #c9b1ff",
        border_style="#c9b1ff",
        padding=(0, 1),
    ),
    tool_result=PanelStyle(
        title_style="bold #95d5b2",
        border_style="#95d5b2",
        padding=(0, 1),
    ),
    tool_error=PanelStyle(
        title_style="bold #f07167",
        border_style="#f07167",
        padding=(0, 1),
    ),
    reasoning=PanelStyle(
        title_style="#6c757d",
        border_style="#6c757d",
        padding=(0, 1),
    ),
    error=PanelStyle(
        title_style="bold #f07167",
        border_style="#f07167",
        padding=(0, 1),
    ),
    welcome=PanelStyle(
        title_style="bold #a0d2db",
        border_style="#a0d2db",
        padding=(0, 1),
    ),
    prompt_style="bold #a0d2db",
    dim_text="#6c757d",
    working_style="italic #6c757d",
    path_highlight="bold #95d5b2",
    usage_style="#6c757d",
    badges=BadgeColors(
        read="#a0d2db",
        write="#c9b1ff",
        execute="#f07167",
        network="#95d5b2",
        external="#6c757d",
    ),
    header=HeaderStyle(
        background="on #16161e",
        text_style="bold #a0d2db",
        separator_style="#3a3a4a",
        separator=SeparatorStyle(style="#3a3a4a"),
        provider_style="bold #95d5b2",
        tokens_style="#6c757d",
        session_style="#6c757d",
        state_style="bold #c9b1ff",
    ),
    footer=FooterStyle(
        background="on #16161e",
        text_style="bold #a0d2db",
        separator_style="#3a3a4a",
        separator=SeparatorStyle(style="#3a3a4a"),
        step_style="#6c757d",
        theme_style="#6c757d",
        input_style="bold #a0d2db",
    ),
    fixed_layout=FixedLayoutConfig(
        regions=[
            FixedLayoutRegion(name="header", size=1),
            FixedLayoutRegion(name="body"),
            FixedLayoutRegion(name="input"),
            FixedLayoutRegion(name="footer", size=1),
        ],
        body_background="#0d1117",
        input_background="#16161e",
        input_field=InputFieldStyle(
            border_color="#3a3a4a",
            border_color_focus="#a0d2db",
            cursor_background="#a0d2db",
            cursor_foreground="#0d1117",
            text_color="#a0d2db",
        ),
        selected_block_style="on #1e2430",
        scrollbar=ScrollbarConfig(
            color="#3a3a4a",
            color_hover="#6c757d",
            color_active="#a0d2db",
            background="#0d1117",
            size=1,
            scroll_speed=4,
        ),
        thinking_panel=ThinkingPanelConfig(
            enabled=True,
            side="right",
            width=38,
            background="#0a0d12",
            border_style="#2a2a3a",
            text_style="italic #6c757d",
            title_style="#3a3a4a",
            scrollbar=ScrollbarConfig(
                color="#2a2a3a",
                color_hover="#3a3a4a",
                color_active="#6c757d",
                background="#0a0d12",
                size=1,
                scroll_speed=4,
            ),
        ),
    ),
)

BUILTIN_THEMES: dict[str, Theme] = {
    t.name: t for t in [BERNSTEIN_THEME, CLASSIC_THEME, DECKER_THEME, SLEEK_THEME]
}
