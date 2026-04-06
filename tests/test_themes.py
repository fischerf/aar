"""Tests for TUI theme system — models, registry, renderer, and fixed-bar TUI."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from agent.core.agent import Agent
from agent.core.config import AgentConfig
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
    BUILTIN_THEMES,
    BERNSTEIN_THEME,
    CLASSIC_THEME,
    DECKER_THEME,
    DEFAULT_THEME,
    SLEEK_THEME,
)
from agent.transports.themes.models import (
    BadgeColors,
    FixedLayoutConfig,
    FixedLayoutRegion,
    FooterStyle,
    HeaderStyle,
    LayoutConfig,
    PanelStyle,
    ScrollbarConfig,
    SectionConfig,
    Theme,
)
from agent.transports.tui import TUIRenderer
from agent.transports.tui_fixed import (
    AarFixedApp,
    ApprovalBar,
    FixedTUIRenderer,
    FooterBar,
    HeaderBar,
    HistoryInput,
    SelectableRichLog,
    _Block,
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
    def test_builtins_registered(self) -> None:
        assert set(BUILTIN_THEMES) == {"default", "contrast", "decker", "sleek"}

    def test_default_matches_bernstein_colors(self) -> None:
        t = DEFAULT_THEME
        assert "#ffb30f" in t.assistant.border_style
        assert "#ff2900" in t.tool_call.border_style
        assert "#ffb30f" in t.tool_result.border_style
        assert "#d12200" in t.error.border_style
        assert t.prompt_style == "bold #ffb30f"

    def test_contrast_uses_classic_colors(self) -> None:
        assert CLASSIC_THEME.assistant.border_style == "green"

    def test_decker_uses_neon(self) -> None:
        assert "#00fff7" in DECKER_THEME.assistant.border_style
        assert "#ff2d95" in DECKER_THEME.tool_call.border_style

    def test_sleek_tight_spacing(self) -> None:
        """Sleek theme uses (0, 1) padding and compact region sizes."""
        assert SLEEK_THEME.assistant.padding == (0, 1)
        assert SLEEK_THEME.tool_call.padding == (0, 1)
        assert SLEEK_THEME.welcome.padding == (0, 1)
        # Compact regions: header=1, footer=1, input/body flexible
        sizes = {r.name: r.size for r in SLEEK_THEME.fixed_layout.regions}
        assert sizes["header"] == 1
        assert sizes["input"] is None
        assert sizes["footer"] == 1
        assert sizes["body"] is None  # flexible
        # Small scrollbar
        assert SLEEK_THEME.fixed_layout.scrollbar.size == 1


# ------------------------------------------------------------------
# ThemeRegistry
# ------------------------------------------------------------------


class TestThemeRegistry:
    def test_get_builtin(self) -> None:
        reg = ThemeRegistry()
        assert reg.get("default").name == "default"
        assert reg.get("contrast").name == "contrast"
        assert reg.get("decker").name == "decker"

    def test_get_unknown_raises(self) -> None:
        reg = ThemeRegistry()
        with pytest.raises(KeyError, match="nonexistent"):
            reg.get("nonexistent")

    def test_list_names_includes_builtins(self) -> None:
        reg = ThemeRegistry()
        names = reg.list_names()
        assert "default" in names
        assert "contrast" in names
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
        renderer, buf = _capture_renderer(CLASSIC_THEME)
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
        renderer.set_theme(CLASSIC_THEME)
        assert renderer.theme.name == "contrast"

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

    def test_contrast_header_uses_classic_colors(self) -> None:
        assert CLASSIC_THEME.header.provider_style == "bold cyan"
        assert "#1a1a2e" in CLASSIC_THEME.header.background

    def test_decker_footer_uses_neon(self) -> None:
        assert "#00fff7" in DECKER_THEME.footer.input_style
        assert "#9d00ff" in DECKER_THEME.footer.theme_style


# ------------------------------------------------------------------
# FixedLayoutConfig models
# ------------------------------------------------------------------


class TestFixedLayoutConfig:
    def test_default_regions(self) -> None:
        fl = FixedLayoutConfig()
        names = [r.name for r in fl.regions]
        assert names == ["header", "body", "input", "footer"]

    def test_body_region_is_flexible(self) -> None:
        fl = FixedLayoutConfig()
        body = next(r for r in fl.regions if r.name == "body")
        assert body.size is None

    def test_header_has_fixed_size(self) -> None:
        fl = FixedLayoutConfig()
        header = next(r for r in fl.regions if r.name == "header")
        assert header.size == 1

    def test_scrollbar_defaults(self) -> None:
        sb = ScrollbarConfig()
        assert sb.enabled is True
        assert sb.size == 1

    def test_builtin_themes_have_fixed_layout(self) -> None:
        for name, theme in BUILTIN_THEMES.items():
            assert isinstance(theme.fixed_layout, FixedLayoutConfig), f"{name} missing fixed_layout"
            assert theme.fixed_layout.body_background != ""

    def test_custom_region_order(self) -> None:
        fl = FixedLayoutConfig(
            regions=[
                FixedLayoutRegion(name="footer", size=2),
                FixedLayoutRegion(name="body"),
                FixedLayoutRegion(name="header", size=2),
            ]
        )
        assert [r.name for r in fl.regions] == ["footer", "body", "header"]

    def test_region_visibility(self) -> None:
        fl = FixedLayoutConfig(
            regions=[
                FixedLayoutRegion(name="header", size=3, visible=False),
                FixedLayoutRegion(name="body"),
                FixedLayoutRegion(name="input", size=3),
                FixedLayoutRegion(name="footer", size=3),
            ]
        )
        visible = [r.name for r in fl.regions if r.visible]
        assert "header" not in visible

    def test_decker_scrollbar_colors(self) -> None:
        sb = DECKER_THEME.fixed_layout.scrollbar
        assert sb.color == "#9d00ff"
        assert sb.color_active == "#00fff7"

    def test_contrast_body_background(self) -> None:
        assert CLASSIC_THEME.fixed_layout.body_background == "#0e0e0e"

    def test_theme_json_roundtrip(self, tmp_path: Path) -> None:
        """Ensure fixed_layout survives JSON serialization."""
        data = DEFAULT_THEME.model_dump()
        p = tmp_path / "test_theme.json"
        p.write_text(json.dumps(data))
        loaded = Theme.model_validate(json.loads(p.read_text()))
        assert loaded.fixed_layout.body_background == DEFAULT_THEME.fixed_layout.body_background
        assert len(loaded.fixed_layout.regions) == 4
        assert loaded.fixed_layout.scrollbar.enabled is True


# ------------------------------------------------------------------
# HeaderBar (Textual widget — render() returns Text)
# ------------------------------------------------------------------


class TestHeaderBar:
    def test_render_contains_provider(self) -> None:
        bar = HeaderBar(DEFAULT_THEME)
        bar.provider_name = "ollama"
        bar.model_name = "llama3"
        rendered = bar.render()
        assert "ollama" in rendered.plain
        assert "llama3" in rendered.plain

    def test_update_tokens(self) -> None:
        bar = HeaderBar(DEFAULT_THEME)
        bar.update_tokens({"input_tokens": 100, "output_tokens": 50})
        assert bar.input_tokens == 100
        assert bar.output_tokens == 50
        bar.update_tokens({"input_tokens": 200, "output_tokens": 30})
        assert bar.input_tokens == 300
        assert bar.output_tokens == 80

    def test_render_contains_session_id(self) -> None:
        bar = HeaderBar(DEFAULT_THEME)
        bar.session_id = "abcdef1234567890"
        rendered = bar.render()
        assert "abcdef12..." in rendered.plain

    def test_render_contains_state(self) -> None:
        bar = HeaderBar(DEFAULT_THEME)
        bar.state = "running"
        rendered = bar.render()
        assert "running" in rendered.plain


# ------------------------------------------------------------------
# FooterBar (Textual widget — render() returns Text)
# ------------------------------------------------------------------


class TestFooterBar:
    def test_render_contains_step(self) -> None:
        bar = FooterBar(DEFAULT_THEME)
        bar.step_count = 42
        rendered = bar.render()
        assert "42" in rendered.plain

    def test_render_contains_theme_name(self) -> None:
        bar = FooterBar(CLASSIC_THEME)
        rendered = bar.render()
        assert "contrast" in rendered.plain

    def test_render_contains_decker(self) -> None:
        bar = FooterBar(DECKER_THEME)
        rendered = bar.render()
        assert "decker" in rendered.plain


# ------------------------------------------------------------------
# FixedTUIRenderer (uses a mock RichLog for testing)
# ------------------------------------------------------------------


class _MockRichLog:
    """Minimal stand-in for ``SelectableRichLog`` in unit tests."""

    def __init__(self) -> None:
        self.items: list = []
        self._blocks: list[_Block] = []
        self._selected_block: int | None = None

    def write(self, content, **kwargs) -> None:  # noqa: ANN001
        self.items.append(content)

    def write_block(self, content, raw: str = "", kind: str = "", **kwargs) -> None:  # noqa: ANN001
        self.items.append(content)
        self._blocks.append(_Block(raw=raw, kind=kind))

    def clear(self) -> None:
        self.items.clear()
        self._blocks.clear()
        self._selected_block = None

    def rendered_text(self) -> str:
        """Render all items via a headless Rich console to plain text."""
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=120)
        for item in self.items:
            console.print(item)
        return buf.getvalue()

    def raw_texts(self) -> list[str]:
        """Return all raw text from tracked blocks."""
        return [b.raw for b in self._blocks]


def _fixed_renderer(
    theme: Theme | None = None, layout: LayoutConfig | None = None
) -> tuple[FixedTUIRenderer, _MockRichLog]:
    theme = theme or DEFAULT_THEME
    log = _MockRichLog()
    header = HeaderBar(theme)
    footer = FooterBar(theme)
    renderer = FixedTUIRenderer(
        log=log,  # type: ignore[arg-type]
        header=header,
        footer=footer,
        verbose=True,
        theme=theme,
        layout=layout,
    )
    return renderer, log


class TestFixedTUIRenderer:
    def test_assistant_message(self) -> None:
        renderer, log = _fixed_renderer()
        renderer.render_event(AssistantMessage(content="hello fixed"))
        output = log.rendered_text()
        assert "Assistant" in output
        assert "hello fixed" in output

    def test_tool_call_increments_footer_step(self) -> None:
        renderer, log = _fixed_renderer()
        renderer.render_event(ToolCall(tool_name="bash", arguments={"cmd": "ls"}))
        assert renderer._footer.step_count == 1
        assert "bash" in log.rendered_text()

    def test_tool_result(self) -> None:
        renderer, log = _fixed_renderer()
        renderer.render_event(ToolResult(tool_name="read_file", output="contents", is_error=False))
        assert "Result: read_file" in log.rendered_text()

    def test_provider_meta_updates_header(self) -> None:
        renderer, log = _fixed_renderer()
        renderer.render_event(
            ProviderMeta(
                model="llama3",
                provider="ollama",
                usage={"input_tokens": 10, "output_tokens": 5},
            )
        )
        assert renderer._header.provider_name == "ollama"
        assert renderer._header.model_name == "llama3"
        assert renderer._header.input_tokens == 10

    def test_hidden_reasoning_suppressed(self) -> None:
        layout = LayoutConfig(reasoning=SectionConfig(visible=False))
        renderer, log = _fixed_renderer(layout=layout)
        renderer.render_event(ReasoningBlock(content="secret thought"))
        assert "secret thought" not in log.rendered_text()

    def test_error_event(self) -> None:
        renderer, log = _fixed_renderer()
        renderer.render_event(ErrorEvent(message="boom", recoverable=True))
        output = log.rendered_text()
        assert "Error" in output
        assert "boom" in output

    def test_welcome(self) -> None:
        renderer, log = _fixed_renderer()
        renderer.render_welcome()
        assert "Aar Agent TUI (Textual)" in log.rendered_text()

    def test_tool_error_uses_error_style(self) -> None:
        renderer, log = _fixed_renderer()
        renderer.render_event(ToolResult(tool_name="bash", output="fail", is_error=True))
        assert "ERROR" in log.rendered_text()

    def test_hidden_token_usage(self) -> None:
        layout = LayoutConfig(token_usage=SectionConfig(visible=False))
        renderer, log = _fixed_renderer(layout=layout)
        renderer.render_event(
            ProviderMeta(
                model="test", provider="test", usage={"input_tokens": 99, "output_tokens": 1}
            )
        )
        # Tokens still tracked in header but usage line not written to log
        assert renderer._header.input_tokens == 99
        assert "99" not in log.rendered_text()


# ------------------------------------------------------------------
# HistoryInput — command history with up/down arrows
# ------------------------------------------------------------------


class TestHistoryInput:
    """Test the HistoryInput widget's history management (no Textual app needed)."""

    def _make_input(self) -> HistoryInput:
        """Create a detached HistoryInput for unit testing."""
        inp = HistoryInput.__new__(HistoryInput)
        inp._history = []
        inp._history_index = -1
        inp._draft = ""
        return inp

    def test_add_to_history(self) -> None:
        inp = self._make_input()
        inp.add_to_history("hello")
        inp.add_to_history("world")
        assert inp._history == ["hello", "world"]

    def test_add_deduplicates_consecutive(self) -> None:
        inp = self._make_input()
        inp.add_to_history("same")
        inp.add_to_history("same")
        assert inp._history == ["same"]

    def test_add_empty_ignored(self) -> None:
        inp = self._make_input()
        inp.add_to_history("")
        inp.add_to_history("   ")
        assert inp._history == []

    def test_history_resets_index_on_add(self) -> None:
        inp = self._make_input()
        inp.add_to_history("first")
        inp._history_index = 0
        inp.add_to_history("second")
        assert inp._history_index == -1


# ------------------------------------------------------------------
# SelectableRichLog — block tracking and copy support
# ------------------------------------------------------------------


class _MockSelectableLog:
    """Minimal stand-in for SelectableRichLog block tracking in unit tests."""

    def __init__(self) -> None:
        self._blocks: list[_Block] = []
        self._selected_block: int | None = None

    def write_block(self, content: object, raw: str = "", kind: str = "", **kw) -> None:  # noqa: ANN003
        self._blocks.append(_Block(raw=raw, kind=kind))

    def write(self, content: object, **kwargs) -> None:  # noqa: ANN003
        pass  # non-block writes (spacers etc.)

    def clear(self) -> None:
        self._blocks.clear()
        self._selected_block = None

    def select_block(self, idx: int | None) -> None:
        self._selected_block = idx

    def get_last_raw(self) -> str:
        if self._blocks:
            return self._blocks[-1].raw
        return ""

    def get_all_text(self) -> str:
        return "\n\n".join(b.raw for b in self._blocks)

    def get_selected_raw(self) -> str:
        if self._selected_block is not None and 0 <= self._selected_block < len(self._blocks):
            return self._blocks[self._selected_block].raw
        return ""


class TestSelectableRichLog:
    def test_tracks_blocks(self) -> None:
        log = _MockSelectableLog()
        log.write_block("block 1", raw="block 1")
        log.write_block("block 2", raw="block 2")
        assert len(log._blocks) == 2

    def test_get_last_raw(self) -> None:
        log = _MockSelectableLog()
        log.write_block("first", raw="first")
        log.write_block("second", raw="second")
        assert log.get_last_raw() == "second"

    def test_get_last_raw_empty(self) -> None:
        log = _MockSelectableLog()
        assert log.get_last_raw() == ""

    def test_get_all_text(self) -> None:
        log = _MockSelectableLog()
        log.write_block("a", raw="a")
        log.write_block("b", raw="b")
        assert "a" in log.get_all_text()
        assert "b" in log.get_all_text()

    def test_selected_block(self) -> None:
        log = _MockSelectableLog()
        log.write_block("alpha", raw="alpha")
        log.write_block("beta", raw="beta")
        log.select_block(0)
        assert log.get_selected_raw() == "alpha"
        log.select_block(1)
        assert log.get_selected_raw() == "beta"

    def test_no_selection_returns_empty(self) -> None:
        log = _MockSelectableLog()
        log.write_block("x", raw="x")
        assert log.get_selected_raw() == ""

    def test_clear_resets(self) -> None:
        log = _MockSelectableLog()
        log.write_block("data", raw="data")
        log.select_block(0)
        log.clear()
        assert log._blocks == []
        assert log._selected_block is None

    def test_raw_text_is_markdown(self) -> None:
        """Verify that copy gives raw markdown, not rendered text."""
        log = _MockSelectableLog()
        log.write_block("rendered panel", raw="# Hello **world**", kind="assistant")
        log.select_block(0)
        assert log.get_selected_raw() == "# Hello **world**"


# ------------------------------------------------------------------
# Thinking toggle
# ------------------------------------------------------------------


class TestThinkingToggle:
    def test_toggle_disables_reasoning(self) -> None:
        renderer, log = _fixed_renderer()
        assert renderer._thinking_visible is True
        result = renderer.toggle_thinking()
        assert result is False
        assert renderer._thinking_visible is False
        # Reasoning events should now be suppressed
        renderer.render_event(ReasoningBlock(content="hidden thought"))
        assert "hidden thought" not in log.rendered_text()

    def test_toggle_re_enables_reasoning(self) -> None:
        renderer, log = _fixed_renderer()
        renderer.toggle_thinking()  # off
        renderer.toggle_thinking()  # on
        assert renderer._thinking_visible is True
        renderer.render_event(ReasoningBlock(content="visible thought"))
        assert "Thinking" in log.rendered_text()

    def test_toggle_updates_header(self) -> None:
        renderer, _ = _fixed_renderer()
        assert renderer._header.thinking_enabled is True
        renderer.toggle_thinking()
        assert renderer._header.thinking_enabled is False


# ------------------------------------------------------------------
# HeaderBar with thinking indicator
# ------------------------------------------------------------------


class TestHeaderBarThinking:
    def test_render_shows_thinking_on(self) -> None:
        bar = HeaderBar(DEFAULT_THEME)
        bar.thinking_enabled = True
        rendered = bar.render()
        assert "think:on" in rendered.plain

    def test_render_shows_thinking_off(self) -> None:
        bar = HeaderBar(DEFAULT_THEME)
        bar.thinking_enabled = False
        rendered = bar.render()
        assert "think:off" in rendered.plain


# ------------------------------------------------------------------
# FooterBar shows key hints
# ------------------------------------------------------------------


class TestFooterBarKeyHints:
    def test_footer_shows_keybindings(self) -> None:
        bar = FooterBar(DEFAULT_THEME)
        rendered = bar.render()
        plain = rendered.plain
        assert "Ctrl+T" in plain
        assert "Ctrl+K" in plain
        assert "Ctrl+L" in plain
        assert "Ctrl+Y" in plain
        assert "Ctrl+Q" in plain


# ------------------------------------------------------------------
# Welcome message includes shortcut hints
# ------------------------------------------------------------------


class TestWelcomeShortcuts:
    def test_welcome_lists_shortcuts(self) -> None:
        renderer, log = _fixed_renderer()
        renderer.render_welcome()
        output = log.rendered_text()
        assert "Ctrl+T" in output
        assert "Ctrl+K" in output
        assert "input history" in output


# ------------------------------------------------------------------
# AarFixedApp integration tests (Textual app startup)
# ------------------------------------------------------------------


def _make_mock_agent() -> Agent:
    """Create a minimal Agent with a stub provider for testing."""
    from unittest.mock import AsyncMock, MagicMock

    from agent.core.config import AgentConfig

    config = AgentConfig()
    provider = MagicMock()
    provider.name = "test"
    provider.supports_audio = False
    provider.supports_reasoning = False
    provider.supports_vision = False
    registry = MagicMock()
    registry.names.return_value = []
    registry.list_tools.return_value = []
    agent = MagicMock(spec=Agent)
    agent.config = config
    agent.provider = provider
    agent.registry = registry
    agent.on_event = MagicMock()
    agent.run = AsyncMock()
    return agent


class TestAarFixedAppStartup:
    """Integration tests: the Textual app actually mounts and renders."""

    @pytest.mark.asyncio
    async def test_app_starts_and_has_widgets(self) -> None:
        agent = _make_mock_agent()
        config = AgentConfig()
        app = AarFixedApp(agent=agent, config=config)
        async with app.run_test(size=(120, 40)) as _pilot:
            # App mounted — check core widgets exist
            assert app.query_one(HeaderBar) is not None
            assert app.query_one(FooterBar) is not None
            assert app.query_one(SelectableRichLog) is not None
            assert app.query_one(HistoryInput) is not None

    @pytest.mark.asyncio
    async def test_app_renders_welcome(self) -> None:
        agent = _make_mock_agent()
        config = AgentConfig()
        app = AarFixedApp(agent=agent, config=config)
        async with app.run_test(size=(120, 40)) as _pilot:
            log = app.query_one("#body-log", SelectableRichLog)
            # Welcome message should be in the blocks (raw text)
            assert any("Aar Agent TUI" in b.raw for b in log._blocks)

    @pytest.mark.asyncio
    async def test_app_header_shows_provider(self) -> None:
        agent = _make_mock_agent()
        config = AgentConfig()
        app = AarFixedApp(agent=agent, config=config)
        async with app.run_test(size=(120, 40)) as _pilot:
            header = app.query_one(HeaderBar)
            rendered = header.render()
            assert config.provider.name in rendered.plain

    @pytest.mark.asyncio
    async def test_app_slash_command_theme_list(self) -> None:
        agent = _make_mock_agent()
        config = AgentConfig()
        app = AarFixedApp(agent=agent, config=config)
        async with app.run_test(size=(120, 40)) as pilot:
            inp = app.query_one("#user-input", HistoryInput)
            inp.value = "/theme"
            await pilot.press("enter")
            await pilot.pause()
            # /theme writes plain text entries (not tracked blocks)
            log = app.query_one("#body-log", SelectableRichLog)
            assert log._total_lines > 0  # content was written

    @pytest.mark.asyncio
    async def test_app_ctrl_t_cycles_theme(self) -> None:
        agent = _make_mock_agent()
        config = AgentConfig()
        app = AarFixedApp(agent=agent, config=config)
        async with app.run_test(size=(120, 40)) as pilot:
            initial_theme = app._renderer.theme.name
            await pilot.press("ctrl+t")
            assert app._renderer.theme.name != initial_theme

    @pytest.mark.asyncio
    async def test_app_ctrl_k_toggles_thinking(self) -> None:
        agent = _make_mock_agent()
        config = AgentConfig()
        app = AarFixedApp(agent=agent, config=config)
        async with app.run_test(size=(120, 40)) as pilot:
            assert app._renderer._thinking_visible is True
            await pilot.press("ctrl+k")
            await pilot.pause()
            assert app._renderer._thinking_visible is False
            await pilot.press("ctrl+k")
            await pilot.pause()
            assert app._renderer._thinking_visible is True

    @pytest.mark.asyncio
    async def test_app_input_history(self) -> None:
        agent = _make_mock_agent()
        config = AgentConfig()
        app = AarFixedApp(agent=agent, config=config)
        async with app.run_test(size=(120, 40)) as pilot:
            inp = app.query_one("#user-input", HistoryInput)
            # Type and submit two slash commands (these don't trigger agent.run)
            inp.value = "/theme"
            await pilot.press("enter")
            await pilot.pause()
            inp.value = "/tools"
            await pilot.press("enter")
            await pilot.pause()
            # History should have two entries
            assert inp._history == ["/theme", "/tools"]

    @pytest.mark.asyncio
    async def test_app_with_each_builtin_theme(self) -> None:
        """Ensure the app starts cleanly with every built-in theme."""
        agent = _make_mock_agent()
        config = AgentConfig()
        for theme_name in ["default", "contrast", "decker", "sleek"]:
            registry = ThemeRegistry()
            theme = registry.get(theme_name)
            app = AarFixedApp(agent=agent, config=config, theme=theme, registry=registry)
            async with app.run_test(size=(120, 40)) as _pilot:
                assert app._renderer.theme.name == theme_name
                header = app.query_one(HeaderBar)
                assert header.theme.name == theme_name


# ------------------------------------------------------------------
# scroll_speed configuration
# ------------------------------------------------------------------


class TestScrollSpeed:
    def test_default_scroll_speed(self) -> None:
        sb = ScrollbarConfig()
        assert sb.scroll_speed == 3

    def test_builtin_themes_have_scroll_speed(self) -> None:
        for name, theme in BUILTIN_THEMES.items():
            assert theme.fixed_layout.scrollbar.scroll_speed > 0, f"{name} missing scroll_speed"

    def test_custom_scroll_speed(self) -> None:
        sb = ScrollbarConfig(scroll_speed=10)
        assert sb.scroll_speed == 10


# ------------------------------------------------------------------
# selected_block_style configuration
# ------------------------------------------------------------------


class TestSelectedBlockStyle:
    def test_default_selected_block_style(self) -> None:
        fl = FixedLayoutConfig()
        assert fl.selected_block_style == "on #2a2a3a"

    def test_builtin_themes_have_selected_block_style(self) -> None:
        for name, theme in BUILTIN_THEMES.items():
            assert theme.fixed_layout.selected_block_style, f"{name} missing selected_block_style"


# ------------------------------------------------------------------
# Raw text copy (renderer stores raw content)
# ------------------------------------------------------------------


class TestRawTextCopy:
    def test_assistant_stores_raw_markdown(self) -> None:
        renderer, log = _fixed_renderer()
        renderer.render_event(AssistantMessage(content="# Hello **world**"))
        raw = [b.raw for b in log._blocks if b.kind == "assistant"]
        assert len(raw) == 1
        assert raw[0] == "# Hello **world**"

    def test_tool_call_stores_raw(self) -> None:
        renderer, log = _fixed_renderer()
        renderer.render_event(ToolCall(tool_name="bash", arguments={"cmd": "ls -la"}))
        raw = [b.raw for b in log._blocks if b.kind == "tool_call"]
        assert len(raw) == 1
        assert "bash" in raw[0]
        assert "ls -la" in raw[0]

    def test_tool_result_stores_raw(self) -> None:
        renderer, log = _fixed_renderer()
        renderer.render_event(
            ToolResult(tool_name="read_file", output="file contents", is_error=False)
        )
        raw = [b.raw for b in log._blocks if b.kind == "tool_result"]
        assert len(raw) == 1
        assert raw[0] == "file contents"

    def test_error_stores_raw(self) -> None:
        renderer, log = _fixed_renderer()
        renderer.render_event(ErrorEvent(message="something broke", recoverable=False))
        raw = [b.raw for b in log._blocks if b.kind == "error"]
        assert len(raw) == 1
        assert raw[0] == "something broke"

    def test_reasoning_stores_raw(self) -> None:
        renderer, log = _fixed_renderer()
        renderer.render_event(ReasoningBlock(content="let me think"))
        raw = [b.raw for b in log._blocks if b.kind == "reasoning"]
        assert len(raw) == 1
        assert raw[0] == "let me think"


# ------------------------------------------------------------------
# No escape-to-quit binding
# ------------------------------------------------------------------


class TestNoEscapeQuit:
    @pytest.mark.asyncio
    async def test_escape_does_not_exit(self) -> None:
        agent = _make_mock_agent()
        config = AgentConfig()
        app = AarFixedApp(agent=agent, config=config)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("escape")
            await pilot.pause()
            # App should still be running — no escape binding
            assert app.is_running


# ------------------------------------------------------------------
# Approval bar
# ------------------------------------------------------------------


class TestApprovalBar:
    @pytest.mark.asyncio
    async def test_approval_bar_exists_on_mount(self) -> None:
        agent = _make_mock_agent()
        config = AgentConfig()
        app = AarFixedApp(agent=agent, config=config)
        async with app.run_test(size=(120, 40)) as _pilot:
            bar = app.query_one(ApprovalBar)
            assert bar is not None
            # Hidden by default
            assert "visible" not in bar.classes

    @pytest.mark.asyncio
    async def test_approval_bar_shows_and_hides(self) -> None:
        agent = _make_mock_agent()
        config = AgentConfig()
        app = AarFixedApp(agent=agent, config=config)
        async with app.run_test(size=(120, 40)) as pilot:
            bar = app.query_one(ApprovalBar)
            event = bar.show_prompt("write_file", "  path: /tmp/test.txt")
            assert "visible" in bar.classes
            await pilot.pause()
            # Simulate clicking Yes
            from textual.widgets import Button

            yes_btn = bar.query_one("#approval-yes", Button)
            await pilot.click(yes_btn)
            await pilot.pause()
            await pilot.pause()
            assert event.is_set()
            from agent.safety.permissions import ApprovalResult

            assert bar.result == ApprovalResult.APPROVED
            assert "visible" not in bar.classes

    @pytest.mark.asyncio
    async def test_approval_bar_buttons_have_readable_labels(self) -> None:
        """All three buttons must be present with full readable labels."""
        agent = _make_mock_agent()
        config = AgentConfig()
        app = AarFixedApp(agent=agent, config=config)
        async with app.run_test(size=(120, 40)) as _pilot:
            bar = app.query_one(ApprovalBar)
            from textual.widgets import Button

            yes_btn = bar.query_one("#approval-yes", Button)
            no_btn = bar.query_one("#approval-no", Button)
            always_btn = bar.query_one("#approval-always", Button)
            assert yes_btn.label.plain == "(y) Yes"
            assert no_btn.label.plain == "(n) No"
            assert always_btn.label.plain == "(a) Always"
            # Buttons must be wide enough to show full label (min-width: 16)
            assert yes_btn.styles.min_width is not None
            assert no_btn.styles.min_width is not None
            assert always_btn.styles.min_width is not None

    @pytest.mark.asyncio
    async def test_approval_bar_prompt_shows_allow(self) -> None:
        """The prompt text must include 'Allow?' after tool details."""
        agent = _make_mock_agent()
        config = AgentConfig()
        app = AarFixedApp(agent=agent, config=config)
        async with app.run_test(size=(120, 40)) as _pilot:
            bar = app.query_one(ApprovalBar)
            bar.show_prompt("bash", "  cmd: rm -rf /")
            assert "visible" in bar.classes
            await _pilot.pause()
            # The approval-text Static should contain "Allow?"
            from textual.widgets import Static as _Static

            text_widget = bar.query_one("#approval-text", _Static)
            # Check rendered output contains Allow?
            rendered = text_widget.render()
            text = rendered.plain if hasattr(rendered, "plain") else str(rendered)
            assert "Allow?" in text

    @pytest.mark.asyncio
    async def test_approval_bar_no_button(self) -> None:
        """Clicking No should resolve with DENIED."""
        from textual.widgets import Button

        from agent.safety.permissions import ApprovalResult

        agent = _make_mock_agent()
        config = AgentConfig()
        app = AarFixedApp(agent=agent, config=config)
        async with app.run_test(size=(120, 40)) as pilot:
            bar = app.query_one(ApprovalBar)
            event = bar.show_prompt("bash", "  cmd: ls")
            await pilot.pause()
            no_btn = bar.query_one("#approval-no", Button)
            await pilot.click(no_btn)
            await pilot.pause()
            await pilot.pause()
            assert event.is_set()
            assert bar.result == ApprovalResult.DENIED
            assert "visible" not in bar.classes

    @pytest.mark.asyncio
    async def test_approval_bar_always_button(self) -> None:
        """Clicking Always should resolve with APPROVED_ALWAYS."""
        from textual.widgets import Button

        from agent.safety.permissions import ApprovalResult

        agent = _make_mock_agent()
        config = AgentConfig()
        app = AarFixedApp(agent=agent, config=config)
        async with app.run_test(size=(120, 40)) as pilot:
            bar = app.query_one(ApprovalBar)
            event = bar.show_prompt("write_file", "  path: /tmp/x")
            await pilot.pause()
            always_btn = bar.query_one("#approval-always", Button)
            await pilot.click(always_btn)
            await pilot.pause()
            await pilot.pause()
            assert event.is_set()
            assert bar.result == ApprovalResult.APPROVED_ALWAYS
            assert "visible" not in bar.classes
