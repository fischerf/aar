"""Tests for TUI theme system — models, registry, and renderer integration."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from agent.core.events import (
    AssistantMessage,
    ErrorEvent,
    ProviderMeta,
    ReasoningBlock,
    ToolCall,
    ToolResult,
)
from agent.transports.themes import ThemeRegistry
from agent.transports.themes.builtin import (
    DECKER_THEME,
    BUILTIN_THEMES,
    CLAUDE_THEME,
    DEFAULT_THEME,
)
from agent.transports.themes.models import (
    BadgeColors,
    LayoutConfig,
    PanelStyle,
    SectionConfig,
    Theme,
)
from agent.transports.tui import TUIRenderer


# ------------------------------------------------------------------
# Model validation
# ------------------------------------------------------------------


class TestThemeModels:
    def test_panel_style_defaults(self) -> None:
        ps = PanelStyle()
        assert ps.title_style == "bold green"
        assert ps.border_style == "green"
        assert ps.padding == (1, 2)

    def test_theme_requires_name(self) -> None:
        with pytest.raises(Exception):
            Theme.model_validate({})

    def test_theme_with_name_only(self) -> None:
        t = Theme(name="minimal")
        assert t.name == "minimal"
        assert t.prompt_style == "bold blue"

    def test_badge_colors_default(self) -> None:
        b = BadgeColors()
        assert b.read == "dim cyan"
        assert b.execute == "red"

    def test_layout_config_defaults(self) -> None:
        lc = LayoutConfig()
        assert lc.welcome.visible is True
        assert lc.reasoning.visible is True
        assert lc.extensions == {}

    def test_section_hidden(self) -> None:
        sc = SectionConfig(visible=False, order=5)
        assert sc.visible is False
        assert sc.order == 5


# ------------------------------------------------------------------
# Built-in themes
# ------------------------------------------------------------------


class TestBuiltinThemes:
    def test_three_builtins_registered(self) -> None:
        assert set(BUILTIN_THEMES) == {"default", "claude", "decker"}

    def test_default_matches_original_colors(self) -> None:
        t = DEFAULT_THEME
        assert t.assistant.border_style == "green"
        assert t.tool_call.border_style == "yellow"
        assert t.tool_result.border_style == "cyan"
        assert t.error.border_style == "red"
        assert t.prompt_style == "bold blue"

    def test_claude_uses_hex_colors(self) -> None:
        assert "#d4a574" in CLAUDE_THEME.assistant.border_style

    def test_decker_uses_neon(self) -> None:
        assert "#00fff7" in DECKER_THEME.assistant.border_style
        assert "#ff2d95" in DECKER_THEME.tool_call.border_style


# ------------------------------------------------------------------
# ThemeRegistry
# ------------------------------------------------------------------


class TestThemeRegistry:
    def test_get_builtin(self) -> None:
        reg = ThemeRegistry()
        assert reg.get("default").name == "default"
        assert reg.get("claude").name == "claude"
        assert reg.get("decker").name == "decker"

    def test_get_unknown_raises(self) -> None:
        reg = ThemeRegistry()
        with pytest.raises(KeyError, match="nonexistent"):
            reg.get("nonexistent")

    def test_list_names_includes_builtins(self) -> None:
        reg = ThemeRegistry()
        names = reg.list_names()
        assert "default" in names
        assert "claude" in names
        assert "decker" in names

    def test_register_custom(self) -> None:
        reg = ThemeRegistry()
        custom = Theme(name="custom")
        reg.register(custom)
        assert reg.get("custom").name == "custom"
        assert "custom" in reg.list_names()

    def test_load_from_json_file(self, tmp_path: Path) -> None:
        data = {"name": "fromfile", "prompt_style": "bold red"}
        p = tmp_path / "fromfile.json"
        p.write_text(json.dumps(data))
        reg = ThemeRegistry()
        theme = reg.get(str(p))
        assert theme.name == "fromfile"
        assert theme.prompt_style == "bold red"


# ------------------------------------------------------------------
# TUIRenderer with themes
# ------------------------------------------------------------------


def _capture_renderer(theme: Theme | None = None, layout: LayoutConfig | None = None) -> tuple[TUIRenderer, StringIO]:
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=120)
    renderer = TUIRenderer(console=console, verbose=True, theme=theme, layout=layout)
    return renderer, buf


class TestTUIRendererThemes:
    def test_default_theme_renders_assistant(self) -> None:
        renderer, buf = _capture_renderer(DEFAULT_THEME)
        event = AssistantMessage(content="hello world")
        renderer.render_event(event)
        output = buf.getvalue()
        assert "Assistant" in output
        assert "hello world" in output

    def test_decker_renders_assistant(self) -> None:
        renderer, buf = _capture_renderer(DECKER_THEME)
        event = AssistantMessage(content="neon reply")
        renderer.render_event(event)
        output = buf.getvalue()
        assert "Assistant" in output
        assert "neon reply" in output

    def test_tool_call_renders(self) -> None:
        renderer, buf = _capture_renderer(CLAUDE_THEME)
        event = ToolCall(tool_name="read_file", arguments={"path": "/tmp/x"})
        renderer.render_event(event)
        output = buf.getvalue()
        assert "read_file" in output

    def test_tool_result_renders(self) -> None:
        renderer, buf = _capture_renderer()
        event = ToolResult(tool_name="read_file", output="file contents", is_error=False)
        renderer.render_event(event)
        output = buf.getvalue()
        assert "Result: read_file" in output

    def test_tool_error_uses_error_style(self) -> None:
        renderer, buf = _capture_renderer()
        event = ToolResult(tool_name="bash", output="fail", is_error=True)
        renderer.render_event(event)
        output = buf.getvalue()
        assert "ERROR" in output

    def test_reasoning_renders(self) -> None:
        renderer, buf = _capture_renderer()
        event = ReasoningBlock(content="let me think")
        renderer.render_event(event)
        output = buf.getvalue()
        assert "Thinking" in output

    def test_error_event_renders(self) -> None:
        renderer, buf = _capture_renderer()
        event = ErrorEvent(message="something broke", recoverable=True)
        renderer.render_event(event)
        output = buf.getvalue()
        assert "Error" in output
        assert "retry" in output

    def test_provider_meta_renders(self) -> None:
        renderer, buf = _capture_renderer()
        event = ProviderMeta(
            model="test", provider="test", usage={"input_tokens": 10, "output_tokens": 5}
        )
        renderer.render_event(event)
        output = buf.getvalue()
        assert "10" in output
        assert "5" in output


# ------------------------------------------------------------------
# Layout visibility
# ------------------------------------------------------------------


class TestLayoutVisibility:
    def test_hidden_reasoning_suppressed(self) -> None:
        layout = LayoutConfig(reasoning=SectionConfig(visible=False))
        renderer, buf = _capture_renderer(layout=layout)
        event = ReasoningBlock(content="hidden thought")
        renderer.render_event(event)
        assert "hidden thought" not in buf.getvalue()

    def test_hidden_token_usage_suppressed(self) -> None:
        layout = LayoutConfig(token_usage=SectionConfig(visible=False))
        renderer, buf = _capture_renderer(layout=layout)
        event = ProviderMeta(
            model="test", provider="test", usage={"input_tokens": 99, "output_tokens": 1}
        )
        renderer.render_event(event)
        assert "99" not in buf.getvalue()

    def test_hidden_assistant_suppressed(self) -> None:
        layout = LayoutConfig(assistant=SectionConfig(visible=False))
        renderer, buf = _capture_renderer(layout=layout)
        event = AssistantMessage(content="invisible")
        renderer.render_event(event)
        assert "invisible" not in buf.getvalue()

    def test_hidden_welcome_suppressed(self) -> None:
        layout = LayoutConfig(welcome=SectionConfig(visible=False))
        renderer, buf = _capture_renderer(layout=layout)
        renderer.render_welcome()
        assert "Aar Agent TUI" not in buf.getvalue()


# ------------------------------------------------------------------
# Theme switching
# ------------------------------------------------------------------


class TestThemeSwitching:
    def test_set_theme_changes_renderer(self) -> None:
        renderer, _ = _capture_renderer(DEFAULT_THEME)
        assert renderer.theme.name == "default"
        renderer.set_theme(CLAUDE_THEME)
        assert renderer.theme.name == "claude"

    def test_cycle_theme(self) -> None:
        reg = ThemeRegistry()
        renderer, _ = _capture_renderer(DEFAULT_THEME)
        renderer.cycle_theme(reg)
        assert renderer.theme.name != "default"

    def test_cycle_wraps_around(self) -> None:
        reg = ThemeRegistry()
        renderer, _ = _capture_renderer(DEFAULT_THEME)
        names = reg.list_names()
        for _ in range(len(names)):
            renderer.cycle_theme(reg)
        assert renderer.theme.name == "default"


# ------------------------------------------------------------------
# Extension panels
# ------------------------------------------------------------------


class TestExtensionPanels:
    def test_register_and_render(self) -> None:
        renderer, buf = _capture_renderer()

        def my_panel(console: Console) -> None:
            console.print("[bold]Extension Output[/]")

        renderer.register_panel("test_ext", my_panel)
        renderer.render_extension_panels()
        assert "Extension Output" in buf.getvalue()

    def test_hidden_extension_suppressed(self) -> None:
        layout = LayoutConfig(
            extensions={"hidden_ext": SectionConfig(visible=False)}
        )
        renderer, buf = _capture_renderer(layout=layout)

        def my_panel(console: Console) -> None:
            console.print("SHOULD NOT APPEAR")

        renderer.register_panel("hidden_ext", my_panel)
        renderer.render_extension_panels()
        assert "SHOULD NOT APPEAR" not in buf.getvalue()
