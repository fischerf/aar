"""Tests for TUI theme system — models, registry, renderer, and fixed-bar TUI."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text

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
    FooterStyle,
    HeaderStyle,
    LayoutConfig,
    PanelStyle,
    SectionConfig,
    Theme,
)
from agent.transports.tui import TUIRenderer
from agent.transports.tui_fixed import (
    ConversationBuffer,
    FixedTUIRenderer,
    FooterBar,
    HeaderBar,
)


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


def _capture_renderer(
    theme: Theme | None = None, layout: LayoutConfig | None = None
) -> tuple[TUIRenderer, StringIO]:
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
        layout = LayoutConfig(extensions={"hidden_ext": SectionConfig(visible=False)})
        renderer, buf = _capture_renderer(layout=layout)

        def my_panel(console: Console) -> None:
            console.print("SHOULD NOT APPEAR")

        renderer.register_panel("hidden_ext", my_panel)
        renderer.render_extension_panels()
        assert "SHOULD NOT APPEAR" not in buf.getvalue()


# ------------------------------------------------------------------
# HeaderStyle / FooterStyle models
# ------------------------------------------------------------------


class TestBarStyleModels:
    def test_header_style_defaults(self) -> None:
        hs = HeaderStyle()
        assert hs.background == "on #1a1a2e"
        assert hs.provider_style == "bold cyan"
        assert hs.tokens_style == "dim green"

    def test_footer_style_defaults(self) -> None:
        fs = FooterStyle()
        assert fs.background == "on #1a1a2e"
        assert fs.input_style == "bold blue"
        assert fs.step_style == "dim cyan"

    def test_theme_has_header_footer(self) -> None:
        t = Theme(name="test")
        assert isinstance(t.header, HeaderStyle)
        assert isinstance(t.footer, FooterStyle)

    def test_builtin_themes_have_header_footer(self) -> None:
        for name, theme in BUILTIN_THEMES.items():
            assert isinstance(theme.header, HeaderStyle), f"{name} missing header"
            assert isinstance(theme.footer, FooterStyle), f"{name} missing footer"

    def test_claude_header_uses_theme_colors(self) -> None:
        assert "#6b9e78" in CLAUDE_THEME.header.provider_style
        assert "#2d2a24" in CLAUDE_THEME.header.background

    def test_decker_footer_uses_neon(self) -> None:
        assert "#00fff7" in DECKER_THEME.footer.input_style
        assert "#9d00ff" in DECKER_THEME.footer.theme_style


# ------------------------------------------------------------------
# ConversationBuffer
# ------------------------------------------------------------------


class TestConversationBuffer:
    def test_append_and_render(self) -> None:
        buf = ConversationBuffer()
        buf.append(Text("hello"))
        buf.append(Text("world"))
        console = Console(file=StringIO(), force_terminal=True, width=80)
        console.print(buf)
        output = console.file.getvalue()
        assert "hello" in output
        assert "world" in output

    def test_clear(self) -> None:
        buf = ConversationBuffer()
        buf.append(Text("data"))
        buf.clear()
        console = Console(file=StringIO(), force_terminal=True, width=80)
        console.print(buf)
        assert "data" not in console.file.getvalue()

    def test_max_items(self) -> None:
        buf = ConversationBuffer(max_items=3)
        for i in range(5):
            buf.append(Text(f"item-{i}"))
        console = Console(file=StringIO(), force_terminal=True, width=80)
        console.print(buf)
        output = console.file.getvalue()
        assert "item-0" not in output
        assert "item-1" not in output
        assert "item-4" in output


# ------------------------------------------------------------------
# HeaderBar
# ------------------------------------------------------------------


class TestHeaderBar:
    def test_renders_provider_info(self) -> None:
        bar = HeaderBar(DEFAULT_THEME)
        bar.provider_name = "ollama"
        bar.model_name = "llama3"
        console = Console(file=StringIO(), force_terminal=True, width=120)
        console.print(bar)
        output = console.file.getvalue()
        assert "ollama" in output
        assert "llama3" in output

    def test_update_tokens(self) -> None:
        bar = HeaderBar(DEFAULT_THEME)
        bar.update_tokens({"input_tokens": 100, "output_tokens": 50})
        assert bar.input_tokens == 100
        assert bar.output_tokens == 50
        bar.update_tokens({"input_tokens": 200, "output_tokens": 30})
        assert bar.input_tokens == 300
        assert bar.output_tokens == 80

    def test_renders_session_id(self) -> None:
        bar = HeaderBar(DEFAULT_THEME)
        bar.session_id = "abcdef1234567890"
        console = Console(file=StringIO(), force_terminal=True, width=120)
        console.print(bar)
        assert "abcdef12..." in console.file.getvalue()

    def test_renders_state(self) -> None:
        bar = HeaderBar(DEFAULT_THEME)
        bar.state = "running"
        console = Console(file=StringIO(), force_terminal=True, width=120)
        console.print(bar)
        assert "running" in console.file.getvalue()


# ------------------------------------------------------------------
# FooterBar
# ------------------------------------------------------------------


class TestFooterBar:
    def test_renders_step_count(self) -> None:
        bar = FooterBar(DEFAULT_THEME)
        bar.step_count = 42
        console = Console(file=StringIO(), force_terminal=True, width=120)
        console.print(bar)
        assert "42" in console.file.getvalue()

    def test_renders_theme_name(self) -> None:
        bar = FooterBar(CLAUDE_THEME)
        console = Console(file=StringIO(), force_terminal=True, width=120)
        console.print(bar)
        assert "claude" in console.file.getvalue()

    def test_renders_status(self) -> None:
        bar = FooterBar(DEFAULT_THEME)
        bar.status = "working..."
        console = Console(file=StringIO(), force_terminal=True, width=120)
        console.print(bar)
        assert "working..." in console.file.getvalue()


# ------------------------------------------------------------------
# FixedTUIRenderer
# ------------------------------------------------------------------


def _fixed_renderer(
    theme: Theme | None = None, layout: LayoutConfig | None = None
) -> tuple[FixedTUIRenderer, ConversationBuffer]:
    theme = theme or DEFAULT_THEME
    buf = ConversationBuffer()
    header = HeaderBar(theme)
    footer = FooterBar(theme)
    renderer = FixedTUIRenderer(
        buffer=buf, header=header, footer=footer, verbose=True, theme=theme, layout=layout
    )
    return renderer, buf


def _render_buf(buf: ConversationBuffer) -> str:
    console = Console(file=StringIO(), force_terminal=True, width=120)
    console.print(buf)
    return console.file.getvalue()


class TestFixedTUIRenderer:
    def test_assistant_message(self) -> None:
        renderer, buf = _fixed_renderer()
        renderer.render_event(AssistantMessage(content="hello fixed"))
        output = _render_buf(buf)
        assert "Assistant" in output
        assert "hello fixed" in output

    def test_tool_call_increments_footer_step(self) -> None:
        renderer, buf = _fixed_renderer()
        renderer.render_event(ToolCall(tool_name="bash", arguments={"cmd": "ls"}))
        assert renderer._footer.step_count == 1
        output = _render_buf(buf)
        assert "bash" in output

    def test_tool_result(self) -> None:
        renderer, buf = _fixed_renderer()
        renderer.render_event(ToolResult(tool_name="read_file", output="contents", is_error=False))
        output = _render_buf(buf)
        assert "Result: read_file" in output

    def test_provider_meta_updates_header(self) -> None:
        renderer, buf = _fixed_renderer()
        renderer.render_event(
            ProviderMeta(
                model="llama3", provider="ollama", usage={"input_tokens": 10, "output_tokens": 5}
            )
        )
        assert renderer._header.provider_name == "ollama"
        assert renderer._header.model_name == "llama3"
        assert renderer._header.input_tokens == 10

    def test_hidden_reasoning_suppressed(self) -> None:
        layout = LayoutConfig(reasoning=SectionConfig(visible=False))
        renderer, buf = _fixed_renderer(layout=layout)
        renderer.render_event(ReasoningBlock(content="secret thought"))
        assert "secret thought" not in _render_buf(buf)

    def test_error_event(self) -> None:
        renderer, buf = _fixed_renderer()
        renderer.render_event(ErrorEvent(message="boom", recoverable=True))
        output = _render_buf(buf)
        assert "Error" in output
        assert "boom" in output

    def test_welcome(self) -> None:
        renderer, buf = _fixed_renderer()
        renderer.render_welcome()
        output = _render_buf(buf)
        assert "Aar Agent TUI (Fixed)" in output

    def test_set_theme_updates_bars(self) -> None:
        renderer, buf = _fixed_renderer(DEFAULT_THEME)
        renderer.set_theme(DECKER_THEME)
        assert renderer.theme.name == "decker"
        assert renderer._header.theme.name == "decker"
        assert renderer._footer.theme_name == "decker"

    def test_cycle_theme(self) -> None:
        reg = ThemeRegistry()
        renderer, buf = _fixed_renderer(DEFAULT_THEME)
        renderer.cycle_theme(reg)
        assert renderer.theme.name != "default"
