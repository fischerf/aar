"""Pydantic models for TUI themes and layout configuration."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PanelStyle(BaseModel):
    """Style definition for a Rich Panel component."""

    title_style: str = "bold green"
    border_style: str = "green"
    padding: tuple[int, int] = (1, 2)


class BadgeColors(BaseModel):
    """Colors for tool side-effect badges."""

    read: str = "dim cyan"
    write: str = "yellow"
    execute: str = "red"
    network: str = "blue"
    external: str = "magenta"


class SeparatorStyle(BaseModel):
    """Style definition for horizontal line separators."""

    character: str = "─"
    style: str = "dim"


class HeaderStyle(BaseModel):
    """Style definition for the fixed header bar."""

    background: str = "on #1a1a2e"
    text_style: str = "bold white"
    separator_style: str = "dim"
    separator: SeparatorStyle = Field(default_factory=SeparatorStyle)
    provider_style: str = "bold cyan"
    tokens_style: str = "dim green"
    tokens_warning_style: str = "bold red"
    session_style: str = "dim"
    state_style: str = "bold yellow"


class FooterStyle(BaseModel):
    """Style definition for the fixed footer bar."""

    background: str = "on #1a1a2e"
    text_style: str = "bold white"
    separator_style: str = "dim"
    separator: SeparatorStyle = Field(default_factory=SeparatorStyle)
    step_style: str = "dim cyan"
    theme_style: str = "dim magenta"
    input_style: str = "bold blue"


class Theme(BaseModel):
    """Complete theme definition for the TUI renderer."""

    name: str
    description: str = ""

    # Panel styles per event type
    assistant: PanelStyle = Field(default_factory=PanelStyle)
    tool_call: PanelStyle = Field(
        default_factory=lambda: PanelStyle(
            title_style="bold yellow", border_style="yellow", padding=(0, 2)
        )
    )
    tool_result: PanelStyle = Field(
        default_factory=lambda: PanelStyle(
            title_style="bold cyan", border_style="cyan", padding=(0, 2)
        )
    )
    tool_error: PanelStyle = Field(
        default_factory=lambda: PanelStyle(
            title_style="bold red", border_style="red", padding=(0, 2)
        )
    )
    reasoning: PanelStyle = Field(
        default_factory=lambda: PanelStyle(title_style="dim", border_style="dim", padding=(0, 2))
    )
    error: PanelStyle = Field(
        default_factory=lambda: PanelStyle(
            title_style="bold red", border_style="red", padding=(0, 2)
        )
    )
    welcome: PanelStyle = Field(
        default_factory=lambda: PanelStyle(
            title_style="bold blue", border_style="blue", padding=(1, 2)
        )
    )

    # Text styles
    prompt_style: str = "bold blue"
    dim_text: str = "dim"
    working_style: str = "dim italic"
    path_highlight: str = "bold blue"
    usage_style: str = "dim"
    usage_warning_style: str = "bold red"

    # Side-effect badges
    badges: BadgeColors = Field(default_factory=BadgeColors)

    # Fixed-bar styles (used by tui_fixed mode)
    header: HeaderStyle = Field(default_factory=HeaderStyle)
    footer: FooterStyle = Field(default_factory=FooterStyle)

    # Full-screen fixed layout (used by tui_fixed mode)
    fixed_layout: FixedLayoutConfig = Field(default_factory=lambda: FixedLayoutConfig())


class FixedLayoutRegion(BaseModel):
    """A single region in the fixed TUI layout."""

    name: str
    size: int | None = None  # None = flexible (fills remaining space)
    visible: bool = True


class ScrollbarConfig(BaseModel):
    """Scrollbar appearance for the fixed TUI body region."""

    enabled: bool = True
    color: str = "#444444"
    color_hover: str = "#666666"
    color_active: str = "#888888"
    background: str = "#1a1a1a"
    background_hover: str = "#222222"
    background_active: str = "#222222"
    size: int = 1
    scroll_speed: int = 3  # lines per scroll tick (mouse wheel / PgUp / PgDn)


class InputFieldStyle(BaseModel):
    """Style for the input text field in the fixed TUI."""

    border_type: str = "tall"  # tall, round, solid, heavy, none, etc.
    border_color: str = "#444444"
    border_color_focus: str = "#888888"
    placeholder_color: str = "#555555"
    cursor_background: str = "#cccccc"
    cursor_foreground: str = "#000000"
    text_color: str = "#ffffff"


class FixedLayoutConfig(BaseModel):
    """Layout configuration for the full-screen fixed TUI.

    Controls region order, sizes, scrollbar appearance, and body background.
    """

    regions: list[FixedLayoutRegion] = Field(
        default_factory=lambda: [
            FixedLayoutRegion(name="header", size=1),
            FixedLayoutRegion(name="body"),
            FixedLayoutRegion(name="input", size=3),
            FixedLayoutRegion(name="footer", size=1),
        ]
    )
    body_background: str = "#0e0e0e"
    input_background: str = "#111118"
    input_field: InputFieldStyle = Field(default_factory=InputFieldStyle)
    selected_block_style: str = "on #2a2a3a"  # highlight color for selected blocks
    scrollbar: ScrollbarConfig = Field(default_factory=ScrollbarConfig)


class SectionConfig(BaseModel):
    """Visibility and ordering for a single TUI section."""

    visible: bool = True
    order: int = 0


class LayoutConfig(BaseModel):
    """Controls which TUI sections are visible and their render order."""

    welcome: SectionConfig = Field(default_factory=lambda: SectionConfig(order=0))
    status_bar: SectionConfig = Field(default_factory=lambda: SectionConfig(order=1))
    reasoning: SectionConfig = Field(default_factory=lambda: SectionConfig(order=10))
    assistant: SectionConfig = Field(default_factory=lambda: SectionConfig(order=20))
    tool_call: SectionConfig = Field(default_factory=lambda: SectionConfig(order=30))
    tool_result: SectionConfig = Field(default_factory=lambda: SectionConfig(order=40))
    token_usage: SectionConfig = Field(default_factory=lambda: SectionConfig(order=50))
    extensions: dict[str, SectionConfig] = Field(default_factory=dict)
