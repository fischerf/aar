"""Full-screen TUI with fixed header/footer, scrollable body, and input widget.

Built on `textual <https://textual.textualize.io>`_ for native scrollbars,
mouse wheel support, Page Up / Page Down, and a proper input line.  The
scrollable TUI (``tui.py``) remains available as the default ``aar tui`` mode;
pass ``--fixed`` to use this one.

Requires the ``tui-fixed`` optional extra::

    pip install "aar-agent[tui-fixed]"
"""

from __future__ import annotations

from typing import Callable

from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.events import Click
    from textual.widgets import Input, RichLog, Static
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "The fixed TUI requires the 'textual' package. "
        'Install it with: pip install "aar-agent[tui-fixed]"'
    ) from exc

from agent.core.agent import Agent
from agent.core.config import AgentConfig
from agent.core.events import (
    AssistantMessage,
    AudioBlock,
    ErrorEvent,
    Event,
    ImageURLBlock,
    ProviderMeta,
    ReasoningBlock,
    ToolCall,
    ToolResult,
)
from agent.core.multimodal import parse_multimodal_input
from agent.core.session import Session
from agent.core.state import AgentState
from agent.memory.session_store import SessionStore
from agent.transports.themes import Theme, ThemeRegistry
from agent.transports.themes.builtin import DEFAULT_THEME
from agent.transports.themes.models import LayoutConfig
from agent.transports.tui import _format_args, _side_effect_badge


# ---------------------------------------------------------------------------
# Textual widgets
# ---------------------------------------------------------------------------


class HeaderBar(Static):
    """Fixed header showing provider, tokens, session, and state."""

    DEFAULT_CSS = """
    HeaderBar {
        dock: top;
        height: 3;
        padding: 0 1;
    }
    """

    def __init__(self, theme: Theme) -> None:
        super().__init__()
        self.theme = theme
        self.provider_name: str = ""
        self.model_name: str = ""
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.session_id: str = ""
        self.state: str = "idle"
        self.thinking_enabled: bool = True

    def update_tokens(self, usage: dict[str, int]) -> None:
        self.input_tokens += usage.get("input_tokens", 0)
        self.output_tokens += usage.get("output_tokens", 0)

    def render(self) -> Text:  # type: ignore[override]
        h = self.theme.header
        provider = f"{self.provider_name}"
        if self.model_name:
            provider += f" / {self.model_name}"
        session = f"{self.session_id[:8]}..." if self.session_id else ""
        thinking_label = "think:on" if self.thinking_enabled else "think:off"
        parts = [
            (provider, h.provider_style),
            ("  |  ", h.separator_style),
            (f"tokens: {self.input_tokens}in / {self.output_tokens}out", h.tokens_style),
            ("  |  ", h.separator_style),
        ]
        if session:
            parts.append((session, h.session_style))
            parts.append(("  |  ", h.separator_style))
        parts.append((self.state, h.state_style))
        parts.append(("  |  ", h.separator_style))
        parts.append((thinking_label, h.tokens_style))
        return Text.assemble(*parts)


class FooterBar(Static):
    """Fixed footer showing step count, theme name, and key hints."""

    DEFAULT_CSS = """
    FooterBar {
        dock: bottom;
        height: 3;
        padding: 0 1;
    }
    """

    def __init__(self, theme: Theme) -> None:
        super().__init__()
        self.theme = theme
        self.step_count: int = 0
        self.theme_name: str = theme.name

    def render(self) -> Text:  # type: ignore[override]
        f = self.theme.footer
        return Text.assemble(
            (f"step: {self.step_count}", f.step_style),
            ("  |  ", f.separator_style),
            (f"theme: {self.theme_name}", f.theme_style),
            ("  |  ", f.separator_style),
            ("Esc", f.step_style),
            (" quit  ", f.separator_style),
            ("Ctrl+T", f.step_style),
            (" theme  ", f.separator_style),
            ("Ctrl+K", f.step_style),
            (" think  ", f.separator_style),
            ("Ctrl+L", f.step_style),
            (" clear  ", f.separator_style),
            ("Ctrl+Y", f.step_style),
            (" copy", f.separator_style),
        )


class SeparatorBar(Static):
    """A thin horizontal line separator."""

    DEFAULT_CSS = """
    SeparatorBar {
        height: 1;
    }
    """

    def __init__(self, style: str = "dim") -> None:
        super().__init__()
        self._style = style

    def render(self) -> Text:  # type: ignore[override]
        return Text("─" * self.size.width, style=self._style)


# ---------------------------------------------------------------------------
# HistoryInput — Input widget with command history (up/down arrows)
# ---------------------------------------------------------------------------


class HistoryInput(Input):
    """Input widget with command history navigation via up/down arrow keys."""

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        self._history: list[str] = []
        self._history_index: int = -1
        self._draft: str = ""  # preserves in-progress input when navigating

    def add_to_history(self, text: str) -> None:
        """Add a command to the history (deduplicates consecutive)."""
        text = text.strip()
        if text and (not self._history or self._history[-1] != text):
            self._history.append(text)
        self._history_index = -1
        self._draft = ""

    def _key_up(self, _event: object) -> None:
        """Navigate to the previous history entry."""
        if not self._history:
            return
        if self._history_index == -1:
            self._draft = self.value
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        self.value = self._history[self._history_index]
        self.cursor_position = len(self.value)

    def _key_down(self, _event: object) -> None:
        """Navigate to the next history entry or back to the draft."""
        if self._history_index == -1:
            return
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.value = self._history[self._history_index]
        else:
            self._history_index = -1
            self.value = self._draft
        self.cursor_position = len(self.value)

    async def _on_key(self, event: object) -> None:
        """Intercept up/down keys for history navigation."""
        key = getattr(event, "key", "")
        if key == "up":
            self._key_up(event)
            if hasattr(event, "prevent_default"):
                event.prevent_default()  # type: ignore[union-attr]
            if hasattr(event, "stop"):
                event.stop()  # type: ignore[union-attr]
        elif key == "down":
            self._key_down(event)
            if hasattr(event, "prevent_default"):
                event.prevent_default()  # type: ignore[union-attr]
            if hasattr(event, "stop"):
                event.stop()  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# SelectableRichLog — RichLog with block selection and copy support
# ---------------------------------------------------------------------------


class SelectableRichLog(RichLog):
    """RichLog that tracks rendered blocks and supports click-to-select + copy."""

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        self._blocks: list[str] = []  # plain-text per block
        self._selected_block: int | None = None

    def write(  # type: ignore[override]
        self,
        content: object,
        width: int | None = None,
        expand: bool = False,
        shrink: bool = True,
        scroll_end: bool | None = None,
        animate: bool = False,
    ) -> SelectableRichLog:
        """Write content and track its plain text for copy support."""
        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        console = Console(file=buf, force_terminal=False, width=200, no_color=True)
        try:
            console.print(content)
        except Exception:
            buf.write(str(content))
        self._blocks.append(buf.getvalue().strip())
        return super().write(  # type: ignore[return-value]
            content,
            width=width,
            expand=expand,
            shrink=shrink,
            scroll_end=scroll_end,
            animate=animate,
        )

    def clear(self) -> None:
        self._blocks.clear()
        self._selected_block = None
        super().clear()

    def get_last_block_text(self) -> str:
        """Get the plain text of the most recent block."""
        if self._blocks:
            return self._blocks[-1]
        return ""

    def get_all_text(self) -> str:
        """Get all blocks as plain text, separated by newlines."""
        return "\n\n".join(self._blocks)

    async def on_click(self, event: Click) -> None:
        """Select the block nearest to the click position for copy."""
        if not self._blocks:
            return
        # Estimate which block was clicked based on Y offset within scroll region.
        # Each block is roughly proportional: map click Y to block index.
        total_lines = max(self.virtual_size.height, 1)
        click_y = event.y + self.scroll_offset.y
        ratio = click_y / total_lines
        idx = int(ratio * len(self._blocks))
        idx = max(0, min(idx, len(self._blocks) - 1))
        self._selected_block = idx

    def get_selected_block_text(self) -> str:
        """Return the text of the currently selected block."""
        if self._selected_block is not None and 0 <= self._selected_block < len(self._blocks):
            return self._blocks[self._selected_block]
        return ""


# ---------------------------------------------------------------------------
# FixedTUIRenderer — renders events into a RichLog
# ---------------------------------------------------------------------------


class FixedTUIRenderer:
    """Renders agent events into a Textual :class:`RichLog` widget."""

    def __init__(
        self,
        log: RichLog | SelectableRichLog,
        header: HeaderBar,
        footer: FooterBar,
        verbose: bool = False,
        theme: Theme | None = None,
        layout: LayoutConfig | None = None,
    ) -> None:
        self._log = log
        self._header = header
        self._footer = footer
        self._verbose = verbose
        self._usage_total: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self._step_count = 0
        self.theme = theme or DEFAULT_THEME
        self.layout = layout or LayoutConfig()
        self._extension_panels: dict[str, Callable] = {}
        self._thinking_visible = True

    # ------------------------------------------------------------------
    # Theme switching
    # ------------------------------------------------------------------

    def set_theme(self, theme: Theme, app: AarFixedApp) -> None:
        """Switch to a new theme and update all widgets."""
        self.theme = theme
        self._header.theme = theme
        self._footer.theme = theme
        self._footer.theme_name = theme.name
        app.apply_theme(theme)
        self._log.write(Text(f"Switched to theme: {theme.name}", style=theme.dim_text))

    def cycle_theme(self, registry: ThemeRegistry, app: AarFixedApp) -> None:
        """Cycle to the next available theme."""
        names = registry.list_names()
        if not names:
            return
        try:
            idx = names.index(self.theme.name)
            next_name = names[(idx + 1) % len(names)]
        except ValueError:
            next_name = names[0]
        self.set_theme(registry.get(next_name), app)

    # ------------------------------------------------------------------
    # Thinking toggle
    # ------------------------------------------------------------------

    def toggle_thinking(self) -> bool:
        """Toggle reasoning block visibility. Returns the new state."""
        self._thinking_visible = not self._thinking_visible
        self._header.thinking_enabled = self._thinking_visible
        self._header.refresh()
        label = "enabled" if self._thinking_visible else "disabled"
        self._log.write(
            Text(f"Thinking display {label}", style=self.theme.dim_text)
        )
        return self._thinking_visible

    # ------------------------------------------------------------------
    # Event rendering (into RichLog)
    # ------------------------------------------------------------------

    def render_event(self, event: Event) -> None:
        """Render a single event into the RichLog widget."""
        t = self.theme

        if isinstance(event, AssistantMessage) and event.content:
            if not self.layout.assistant.visible:
                return
            self._log.write(Text())
            self._log.write(
                Panel(
                    Markdown(event.content),
                    title=f"[{t.assistant.title_style}]Assistant[/]",
                    border_style=t.assistant.border_style,
                    padding=t.assistant.padding,
                )
            )

        elif isinstance(event, ToolCall):
            self._step_count += 1
            self._footer.step_count = self._step_count
            self._footer.refresh()
            if not self.layout.tool_call.visible:
                return
            args_display = _format_args(event.arguments, verbose=self._verbose, theme=t)
            if self._verbose:
                badge = _side_effect_badge(event.data.get("side_effects", []), theme=t)
                badge_prefix = f"{badge} " if badge else ""
                title = (
                    f"{badge_prefix}[{t.tool_call.title_style}]{event.tool_name}[/]"
                    f" [{t.dim_text}](step {self._step_count})[/]"
                )
            else:
                title = (
                    f"[{t.tool_call.title_style}]Tool: {event.tool_name}[/]"
                    f" [{t.dim_text}](step {self._step_count})[/]"
                )
            self._log.write(
                Panel(
                    args_display,
                    title=title,
                    border_style=t.tool_call.border_style,
                    padding=t.tool_call.padding,
                )
            )

        elif isinstance(event, ToolResult):
            if not self.layout.tool_result.visible:
                return
            ps = t.tool_error if event.is_error else t.tool_result
            output = event.output
            if len(output) > 1000:
                output = output[:1000] + "\n... (truncated)"
            if self._verbose and event.duration_ms > 0:
                duration = f" [{t.dim_text}]{event.duration_ms:.0f}ms[/]"
            else:
                duration = ""
            title = f"[{ps.title_style}]Result: {event.tool_name}[/]{duration}"
            if event.is_error:
                title += f" [{t.tool_error.border_style}]ERROR[/]"
            self._log.write(
                Panel(output, title=title, border_style=ps.border_style, padding=ps.padding)
            )

        elif isinstance(event, ReasoningBlock) and event.content:
            if not self.layout.reasoning.visible or not self._thinking_visible:
                return
            text = event.content
            if len(text) > 500:
                text = text[:500] + "..."
            self._log.write(
                Panel(
                    Text(text, style=f"italic {t.reasoning.border_style}"),
                    title=f"[{t.reasoning.title_style}]Thinking[/]",
                    border_style=t.reasoning.border_style,
                    padding=t.reasoning.padding,
                )
            )

        elif isinstance(event, ErrorEvent):
            hint = (
                f"\n[{t.dim_text}]You can type your message again to retry.[/]"
                if event.recoverable
                else ""
            )
            self._log.write(
                Panel(
                    event.message + hint,
                    title=f"[{t.error.title_style}]Error[/]",
                    border_style=t.error.border_style,
                    padding=t.error.padding,
                )
            )

        elif isinstance(event, ProviderMeta):
            u = event.usage
            self._usage_total["input_tokens"] += u.get("input_tokens", 0)
            self._usage_total["output_tokens"] += u.get("output_tokens", 0)
            self._header.update_tokens(u)
            self._header.provider_name = event.provider
            self._header.model_name = event.model
            self._header.refresh()
            if not self.layout.token_usage.visible:
                return
            self._log.write(
                Text(
                    f"  {u.get('input_tokens', 0)}in / {u.get('output_tokens', 0)}out "
                    f"(total: {self._usage_total['input_tokens']}in"
                    f" / {self._usage_total['output_tokens']}out)",
                    style=t.usage_style,
                )
            )

    def render_welcome(self) -> None:
        if not self.layout.welcome.visible:
            return
        t = self.theme
        self._log.write(
            Panel(
                "[bold]Aar Agent TUI (Fixed)[/]\n\n"
                "Type your message and press Enter.\n"
                "Attach files with @path (e.g. @photo.jpg @audio.wav)\n"
                "Commands: [bold]/quit[/] [bold]/status[/] [bold]/tools[/] "
                "[bold]/policy[/] [bold]/theme[/] [bold]/clear[/]\n\n"
                "[bold]Shortcuts:[/]\n"
                "  [bold]Ctrl+T[/]  cycle theme    "
                "[bold]Ctrl+K[/]  toggle thinking\n"
                "  [bold]Ctrl+L[/]  clear screen   "
                "[bold]Ctrl+Y[/]  copy last block\n"
                "  [bold]Escape[/]  quit           "
                "[bold]PgUp/PgDn[/]  scroll\n"
                "  [bold]↑ / ↓[/]   input history  "
                "[bold]Click[/]  select block",
                border_style=t.welcome.border_style,
                padding=t.welcome.padding,
            )
        )


# ---------------------------------------------------------------------------
# Textual App
# ---------------------------------------------------------------------------


class AarFixedApp(App):
    """Full-screen Textual application for the fixed TUI mode."""

    BINDINGS = [
        Binding("escape", "quit", "Quit"),
        Binding("pageup", "scroll_up", "Page Up", show=False),
        Binding("pagedown", "scroll_down", "Page Down", show=False),
        Binding("ctrl+t", "cycle_theme", "Cycle theme", show=False),
        Binding("ctrl+k", "toggle_thinking", "Toggle thinking", show=False, priority=True),
        Binding("ctrl+l", "clear_screen", "Clear screen", show=False),
        Binding("ctrl+y", "copy_block", "Copy selected block", show=False),
    ]

    CSS = """
    Screen {
        layout: vertical;
    }
    #header-sep {
        height: 1;
    }
    #body-log {
        min-height: 4;
    }
    #input-sep {
        height: 1;
    }
    #user-input {
        height: 3;
        padding: 0 1;
    }
    #footer-sep {
        height: 1;
    }
    """

    def __init__(
        self,
        agent: Agent,
        config: AgentConfig,
        renderer: FixedTUIRenderer | None = None,
        theme: Theme | None = None,
        layout_config: LayoutConfig | None = None,
        registry: ThemeRegistry | None = None,
        verbose: bool = False,
        session_id: str | None = None,
    ) -> None:
        super().__init__()
        self._agent = agent
        self._config = config
        self._theme = theme or DEFAULT_THEME
        self._layout_config = layout_config or LayoutConfig()
        self._theme_registry = registry or ThemeRegistry()
        self._verbose = verbose
        self._session_id = session_id
        self._session: Session | None = None
        self._store = SessionStore(config.session_dir)
        self._renderer: FixedTUIRenderer | None = renderer

    # ------------------------------------------------------------------
    # Compose the widget tree from theme layout config
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        fl = self._theme.fixed_layout

        # Map region names to widget factories
        widget_map: dict[str, Callable[[], list]] = {
            "header": lambda: [
                HeaderBar(self._theme),
                SeparatorBar(self._theme.header.separator_style),
            ],
            "body": lambda: [
                SelectableRichLog(id="body-log", wrap=True, markup=True, auto_scroll=True),
            ],
            "input": lambda: [
                SeparatorBar(self._theme.footer.separator_style),
                HistoryInput(placeholder="> type your message...", id="user-input"),
            ],
            "footer": lambda: [
                SeparatorBar(self._theme.footer.separator_style),
                FooterBar(self._theme),
            ],
        }

        for region in fl.regions:
            if not region.visible:
                continue
            factory = widget_map.get(region.name)
            if factory:
                yield from factory()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        log = self.query_one("#body-log", SelectableRichLog)
        header = self.query_one(HeaderBar)
        footer = self.query_one(FooterBar)

        # Populate header from config
        header.provider_name = self._config.provider.name
        header.model_name = self._config.provider.model

        self._renderer = FixedTUIRenderer(
            log=log,
            header=header,
            footer=footer,
            verbose=self._verbose,
            theme=self._theme,
            layout=self._layout_config,
        )
        self._agent.on_event(self._renderer.render_event)

        # Apply theme CSS
        self.apply_theme(self._theme)

        # Resume session if requested
        if self._session_id:
            try:
                self._session = self._store.load(self._session_id)
                header.session_id = self._session.session_id
                header.refresh()
                log.write(Text(f"Resumed session {self._session_id}", style=self._theme.dim_text))
            except FileNotFoundError:
                log.write(
                    Text(
                        f"Session {self._session_id} not found",
                        style=self._theme.error.border_style,
                    )
                )

        self._renderer.render_welcome()

        # Focus the input
        self.query_one("#user-input", HistoryInput).focus()

    def apply_theme(self, theme: Theme) -> None:
        """Apply theme colors to Textual widget styles."""
        self._theme = theme
        fl = theme.fixed_layout
        sb = fl.scrollbar

        # Body log
        try:
            log = self.query_one("#body-log", SelectableRichLog)
            log.styles.background = fl.body_background
            log.styles.scrollbar_color = sb.color
            log.styles.scrollbar_color_hover = sb.color_hover
            log.styles.scrollbar_color_active = sb.color_active
            log.styles.scrollbar_background = sb.background
            log.styles.scrollbar_background_hover = sb.background_hover
            log.styles.scrollbar_background_active = sb.background_active
            log.styles.scrollbar_size_vertical = sb.size
        except Exception:
            pass

        # Input
        try:
            inp = self.query_one("#user-input", HistoryInput)
            inp.styles.background = fl.input_background
        except Exception:
            pass

        # Header
        try:
            header = self.query_one(HeaderBar)
            header.styles.background = theme.header.background.replace("on ", "")
        except Exception:
            pass

        # Footer
        try:
            footer = self.query_one(FooterBar)
            footer.styles.background = theme.footer.background.replace("on ", "")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Key binding actions
    # ------------------------------------------------------------------

    def action_scroll_up(self) -> None:
        self.query_one("#body-log", SelectableRichLog).scroll_page_up()

    def action_scroll_down(self) -> None:
        self.query_one("#body-log", SelectableRichLog).scroll_page_down()

    def action_cycle_theme(self) -> None:
        """Ctrl+T — cycle to the next theme."""
        if self._renderer:
            self._renderer.cycle_theme(self._theme_registry, self)

    def action_toggle_thinking(self) -> None:
        """Ctrl+K — toggle reasoning/thinking block visibility."""
        if self._renderer:
            self._renderer.toggle_thinking()

    def action_clear_screen(self) -> None:
        """Ctrl+L — clear the scrollback and reset counters."""
        if not self._renderer:
            return
        log = self.query_one("#body-log", SelectableRichLog)
        header = self.query_one(HeaderBar)
        footer = self.query_one(FooterBar)
        log.clear()
        self._session = None
        header.session_id = ""
        header.input_tokens = 0
        header.output_tokens = 0
        header.state = "idle"
        header.refresh()
        footer.step_count = 0
        footer.refresh()
        self._renderer._step_count = 0
        self._renderer._usage_total = {"input_tokens": 0, "output_tokens": 0}
        self._renderer.render_welcome()

    def action_copy_block(self) -> None:
        """Ctrl+Y — copy the selected (or last) block to the clipboard."""
        log = self.query_one("#body-log", SelectableRichLog)
        text = log.get_selected_block_text()
        if not text:
            text = log.get_last_block_text()
        if text:
            self.copy_to_clipboard(text)
            if self._renderer:
                self._renderer._log.write(
                    Text("Copied to clipboard", style=self._renderer.theme.dim_text)
                )

    # ------------------------------------------------------------------
    # Scroll-aware auto_scroll: pause on manual scroll, resume at bottom
    # ------------------------------------------------------------------

    def on_rich_log_scroll(self) -> None:
        """When the user scrolls, pause auto-scroll if not at the bottom."""
        try:
            log = self.query_one("#body-log", SelectableRichLog)
            at_bottom = log.scroll_offset.y >= (log.virtual_size.height - log.size.height - 2)
            log.auto_scroll = at_bottom
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Input handling
    # ------------------------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle user input from the Input widget."""
        user_input = event.value
        inp = self.query_one("#user-input", HistoryInput)
        inp.value = ""
        stripped = user_input.strip()
        if not stripped:
            return

        # Add to input history
        inp.add_to_history(stripped)

        assert self._renderer is not None
        log = self.query_one("#body-log", SelectableRichLog)
        header = self.query_one(HeaderBar)

        # Echo input
        log.write(Text(f"  > {stripped}", style=self._renderer.theme.prompt_style))

        # --- TUI commands ------------------------------------------------
        if stripped.lower() in {"/quit", "/exit", "/q"}:
            self.exit()
            return
        elif stripped.lower() == "/status" and self._session:
            t = self._renderer.theme
            log.write(
                Text(
                    f"Session: {self._session.session_id[:8]}... | "
                    f"Steps: {self._session.step_count} | "
                    f"State: {self._session.state.value}",
                    style=t.dim_text,
                )
            )
            return
        elif stripped.lower() == "/tools":
            for spec in self._agent.registry.list_tools():
                effects = ", ".join(e.value for e in spec.side_effects)
                log.write(Text(f"  {spec.name} ({effects})  {spec.description}"))
            return
        elif stripped.lower() == "/policy":
            sc = self._config.safety
            log.write(Text("Safety Policy", style="bold"))
            for line in [
                f"  read_only: {sc.read_only}",
                f"  require_approval_for_writes: {sc.require_approval_for_writes}",
                f"  require_approval_for_execute: {sc.require_approval_for_execute}",
                f"  sandbox: {sc.sandbox}",
            ]:
                log.write(Text(line))
            return
        elif stripped.lower() == "/clear":
            self.action_clear_screen()
            return
        elif stripped.lower().startswith("/theme"):
            parts = stripped.split(maxsplit=1)
            if len(parts) == 1:
                log.write(
                    Text(
                        f"Current theme: {self._renderer.theme.name}",
                        style=self._renderer.theme.dim_text,
                    )
                )
                for tname in self._theme_registry.list_names():
                    marker = " *" if tname == self._renderer.theme.name else ""
                    log.write(Text(f"  {tname}{marker}", style=self._renderer.theme.dim_text))
            else:
                arg = parts[1].strip()
                if arg == "next":
                    self._renderer.cycle_theme(self._theme_registry, self)
                else:
                    try:
                        self._renderer.set_theme(self._theme_registry.get(arg), self)
                    except KeyError:
                        log.write(
                            Text(
                                f"Unknown theme: {arg}",
                                style=self._renderer.theme.error.border_style,
                            )
                        )
            return
        elif stripped.lower() == "/think":
            self._renderer.toggle_thinking()
            return
        elif stripped.lower() == "/copy":
            self.action_copy_block()
            return

        # --- Parse multimodal attachments --------------------------------
        content = parse_multimodal_input(stripped)
        if isinstance(content, list):
            has_audio = False
            for block in content:
                if isinstance(block, ImageURLBlock):
                    log.write(Text("  Attached: image", style=self._renderer.theme.dim_text))
                elif isinstance(block, AudioBlock):
                    log.write(Text("  Attached: audio", style=self._renderer.theme.dim_text))
                    has_audio = True
            if has_audio and not self._agent.provider.supports_audio:
                log.write(
                    Text(
                        f"Warning: audio input is not supported by "
                        f"{self._agent.provider.name}. Audio will be dropped.",
                        style=self._renderer.theme.badges.write,
                    )
                )

        # --- Run agent ---------------------------------------------------
        header.state = "running"
        header.refresh()
        inp.placeholder = "working..."
        inp.disabled = True
        # Re-enable auto-scroll when starting agent work
        log.auto_scroll = True

        self._session = await self._agent.run(content, self._session)

        header.state = self._session.state.value
        header.session_id = self._session.session_id
        header.refresh()
        inp.placeholder = "> type your message..."
        inp.disabled = False
        inp.focus()

        # Handle recoverable errors
        if self._session.state == AgentState.ERROR:
            last_error = next(
                (e for e in reversed(self._session.events) if isinstance(e, ErrorEvent)),
                None,
            )
            if last_error and last_error.recoverable:
                self._session.state = AgentState.COMPLETED
                header.state = "completed"
                header.refresh()

        self._store.save(self._session)

    def on_unmount(self) -> None:
        if self._session:
            self._store.save(self._session)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run_tui_fixed(
    config: AgentConfig | None = None,
    agent: Agent | None = None,
    verbose: bool = False,
    session_id: str | None = None,
    theme_name: str | None = None,
) -> None:
    """Launch the full-screen TUI with fixed header/footer bars."""
    config = config or AgentConfig()

    registry = ThemeRegistry()
    name = theme_name or config.tui.theme
    try:
        theme = registry.get(name)
    except KeyError:
        theme = DEFAULT_THEME

    layout_config = (
        LayoutConfig.model_validate(config.tui.layout) if config.tui.layout else LayoutConfig()
    )

    agent = agent or Agent(config=config)

    app = AarFixedApp(
        agent=agent,
        config=config,
        theme=theme,
        layout_config=layout_config,
        registry=registry,
        verbose=verbose,
        session_id=session_id,
    )
    await app.run_async()
