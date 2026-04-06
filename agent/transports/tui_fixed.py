"""Full-screen TUI with fixed header/footer, scrollable body, and input widget.

Built on `textual <https://textual.textualize.io>`_ for native scrollbars,
mouse wheel support, Page Up / Page Down, and a proper input line.  The
scrollable TUI (``tui.py``) remains available as the default ``aar tui`` mode;
pass ``--fixed`` to use this one.

Requires the ``tui-fixed`` optional extra::

    pip install "aar-agent[tui-fixed]"
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable

from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal
    from textual.events import Click
    from textual.widgets import Button, Input, RichLog, Static
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
from agent.safety.permissions import ApprovalResult
from agent.transports.themes import Theme, ThemeRegistry
from agent.transports.themes.builtin import DEFAULT_THEME
from agent.transports.themes.models import LayoutConfig
from agent.transports.tui import _format_args, _side_effect_badge

# ---------------------------------------------------------------------------
# Block — tracks raw content for each rendered block
# ---------------------------------------------------------------------------


@dataclass
class _Block:
    """A single rendered block in the scrollable body."""

    raw: str  # raw text / markdown (what gets copied)
    kind: str = ""  # "assistant", "tool_call", "tool_result", "reasoning", "error", etc.
    line_start: int = 0  # first line index in the RichLog
    line_count: int = 0  # how many lines this block occupies


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
            ("Ctrl+T", f.step_style),
            (" theme  ", f.separator_style),
            ("Ctrl+K", f.step_style),
            (" think  ", f.separator_style),
            ("Ctrl+L", f.step_style),
            (" clear  ", f.separator_style),
            ("Ctrl+Y", f.step_style),
            (" copy  ", f.separator_style),
            ("Ctrl+Q", f.step_style),
            (" exit", f.separator_style),
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
# ApprovalBar — inline prompt for tool approval (yes / no / always)
# ---------------------------------------------------------------------------


class ApprovalBar(Static):
    """Inline approval prompt shown when a tool requires user consent."""

    DEFAULT_CSS = """
    ApprovalBar {
        height: auto;
        max-height: 12;
        padding: 0 1;
        display: none;
    }
    ApprovalBar.visible {
        display: block;
    }
    ApprovalBar .approval-buttons {
        height: 3;
    }
    ApprovalBar Button {
        min-width: 16;
        margin: 0 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._tool_text: str = ""
        self._result_event: asyncio.Event | None = None
        self._result: ApprovalResult = ApprovalResult.DENIED

    def compose(self) -> ComposeResult:  # type: ignore[override]
        yield Static("", id="approval-text")
        with Horizontal(classes="approval-buttons"):
            yield Button("(y) Yes", id="approval-yes", variant="success")
            yield Button("(n) No", id="approval-no", variant="error")
            yield Button("(a) Always", id="approval-always", variant="warning")

    def show_prompt(self, tool_name: str, args_text: str) -> asyncio.Event:
        """Display an approval prompt. Returns an asyncio.Event that is set when answered."""
        self._result_event = asyncio.Event()
        self._result = ApprovalResult.DENIED
        text_widget = self.query_one("#approval-text", Static)
        text_widget.update(
            f"[bold red]Approval Required[/]\n[bold]{tool_name}[/]\n{args_text}\n[bold]Allow?[/]"
        )
        self.add_class("visible")
        # Focus the Yes button by default
        try:
            self.query_one("#approval-yes", Button).focus()
        except Exception:
            pass
        return self._result_event

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button clicks for approval decisions."""
        if event.button.id == "approval-yes":
            self._resolve(ApprovalResult.APPROVED)
        elif event.button.id == "approval-always":
            self._resolve(ApprovalResult.APPROVED_ALWAYS)
        else:
            self._resolve(ApprovalResult.DENIED)

    async def on_key(self, event: object) -> None:
        """Allow y/n/a keyboard shortcuts when visible."""
        if "visible" not in self.classes:
            return
        key = getattr(event, "character", "")
        if key == "y":
            self._resolve(ApprovalResult.APPROVED)
        elif key == "n":
            self._resolve(ApprovalResult.DENIED)
        elif key == "a":
            self._resolve(ApprovalResult.APPROVED_ALWAYS)
        else:
            return
        if hasattr(event, "prevent_default"):
            event.prevent_default()  # type: ignore[union-attr]
        if hasattr(event, "stop"):
            event.stop()  # type: ignore[union-attr]

    def _resolve(self, result: ApprovalResult) -> None:
        """Set the result, hide the bar, and signal completion."""
        self._result = result
        self.remove_class("visible")
        if self._result_event:
            self._result_event.set()

    @property
    def result(self) -> ApprovalResult:
        return self._result


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
# SelectableRichLog — RichLog with block selection, highlighting, and copy
# ---------------------------------------------------------------------------


class SelectableRichLog(RichLog):
    """RichLog that tracks rendered blocks and supports click-to-select + copy.

    Each ``write()`` call is paired with a raw text string via ``write_block()``.
    Left-click selects and highlights a block; right-click copies and deselects.
    """

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        self._blocks: list[_Block] = []
        self._selected_block: int | None = None
        self._total_lines: int = 0
        self._selected_style: str = "on #2a2a3a"  # overridden by theme

    def write_block(
        self,
        content: object,
        raw: str,
        kind: str = "",
        **kwargs,  # noqa: ANN003
    ) -> SelectableRichLog:
        """Write a renderable to the log and track its raw text for copy."""
        line_start = self._total_lines
        result = self.write(content, **kwargs)
        # Estimate lines by measuring the rendered content
        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        console = Console(file=buf, force_terminal=False, width=max(self.size.width - 4, 40))
        try:
            console.print(content)
        except Exception:
            buf.write(str(content))
        line_count = max(buf.getvalue().count("\n"), 1)
        self._blocks.append(
            _Block(raw=raw, kind=kind, line_start=line_start, line_count=line_count)
        )
        self._total_lines += line_count
        return result  # type: ignore[return-value]

    def write(  # type: ignore[override]
        self,
        content: object,
        width: int | None = None,
        expand: bool = False,
        shrink: bool = True,
        scroll_end: bool | None = None,
        animate: bool = False,
    ) -> SelectableRichLog:
        """Write content (used internally by Textual deferred renders)."""
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
        self._total_lines = 0
        super().clear()

    def _block_at_y(self, click_y: int) -> int | None:
        """Find which block index contains the given Y coordinate."""
        if not self._blocks:
            return None
        y = click_y + self.scroll_offset.y
        total = max(self.virtual_size.height, 1)
        ratio = y / total
        idx = int(ratio * len(self._blocks))
        return max(0, min(idx, len(self._blocks) - 1))

    def select_block(self, idx: int | None) -> None:
        """Select a block by index (or deselect with None)."""
        old = self._selected_block
        self._selected_block = idx
        # Re-render affected blocks to show/hide highlight
        if old is not None or idx is not None:
            self.refresh()

    async def on_click(self, event: Click) -> None:
        """Left-click selects a block; right-click copies and deselects."""
        idx = self._block_at_y(event.y)
        if event.button == 3:  # right-click
            if self._selected_block is not None:
                # Copy will be handled by the app's action
                app = self.app
                if hasattr(app, "_do_copy_selected"):
                    app._do_copy_selected()  # type: ignore[attr-defined]
            return
        # Left-click — select or deselect
        if idx == self._selected_block:
            self.select_block(None)
        else:
            self.select_block(idx)

    def get_selected_raw(self) -> str:
        """Return the raw text of the currently selected block."""
        if self._selected_block is not None and 0 <= self._selected_block < len(self._blocks):
            return self._blocks[self._selected_block].raw
        return ""

    def get_last_raw(self) -> str:
        """Return the raw text of the most recent block."""
        if self._blocks:
            return self._blocks[-1].raw
        return ""

    def get_all_text(self) -> str:
        """Get all blocks' raw text, separated by newlines."""
        return "\n\n".join(b.raw for b in self._blocks)


# ---------------------------------------------------------------------------
# FixedTUIRenderer — renders events into a RichLog
# ---------------------------------------------------------------------------


class FixedTUIRenderer:
    """Renders agent events into a Textual :class:`SelectableRichLog` widget."""

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

    def _write(self, content: object, raw: str = "", kind: str = "") -> None:
        """Write to the log, using block tracking when available."""
        if hasattr(self._log, "write_block") and raw:
            self._log.write_block(content, raw=raw, kind=kind)
        else:
            self._log.write(content)

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
        self._write(
            Text(f"Switched to theme: {theme.name}", style=theme.dim_text),
            raw=f"Switched to theme: {theme.name}",
            kind="system",
        )

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
        self._write(
            Text(f"Thinking display {label}", style=self.theme.dim_text),
            raw=f"Thinking display {label}",
            kind="system",
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
            self._log.write(Text())  # spacer
            self._write(
                Panel(
                    Markdown(event.content),
                    title=f"[{t.assistant.title_style}]Assistant[/]",
                    border_style=t.assistant.border_style,
                    padding=t.assistant.padding,
                ),
                raw=event.content,
                kind="assistant",
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
            # Raw text: tool name + arguments as readable string
            import json

            try:
                raw_args = json.dumps(event.arguments, indent=2)
            except Exception:
                raw_args = str(event.arguments)
            self._write(
                Panel(
                    args_display,
                    title=title,
                    border_style=t.tool_call.border_style,
                    padding=t.tool_call.padding,
                ),
                raw=f"Tool: {event.tool_name}\n{raw_args}",
                kind="tool_call",
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
            self._write(
                Panel(output, title=title, border_style=ps.border_style, padding=ps.padding),
                raw=output,
                kind="tool_result",
            )

        elif isinstance(event, ReasoningBlock) and event.content:
            if not self.layout.reasoning.visible or not self._thinking_visible:
                return
            text = event.content
            if len(text) > 500:
                text = text[:500] + "..."
            self._write(
                Panel(
                    Text(text, style=f"italic {t.reasoning.border_style}"),
                    title=f"[{t.reasoning.title_style}]Thinking[/]",
                    border_style=t.reasoning.border_style,
                    padding=t.reasoning.padding,
                ),
                raw=text,
                kind="reasoning",
            )

        elif isinstance(event, ErrorEvent):
            hint = (
                f"\n[{t.dim_text}]You can type your message again to retry.[/]"
                if event.recoverable
                else ""
            )
            self._write(
                Panel(
                    event.message + hint,
                    title=f"[{t.error.title_style}]Error[/]",
                    border_style=t.error.border_style,
                    padding=t.error.padding,
                ),
                raw=event.message,
                kind="error",
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
            usage_text = (
                f"  {u.get('input_tokens', 0)}in / {u.get('output_tokens', 0)}out "
                f"(total: {self._usage_total['input_tokens']}in"
                f" / {self._usage_total['output_tokens']}out)"
            )
            self._write(
                Text(usage_text, style=t.usage_style),
                raw=usage_text.strip(),
                kind="usage",
            )

    def render_welcome(self) -> None:
        if not self.layout.welcome.visible:
            return
        t = self.theme
        welcome_text = (
            "[bold]Aar Agent TUI (Fixed)[/]\n\n"
            "Type your message and press Enter.\n"
            "Attach files with @path (e.g. @photo.jpg @audio.wav)\n"
            "Commands: [bold]/quit[/] [bold]/status[/] [bold]/tools[/] "
            "[bold]/policy[/] [bold]/theme[/] [bold]/clear[/]\n\n"
            "[bold]Shortcuts:[/]\n"
            "  [bold]Ctrl+T[/]  cycle theme    "
            "[bold]Ctrl+K[/]  toggle thinking\n"
            "  [bold]Ctrl+L[/]  clear screen   "
            "[bold]Ctrl+Y[/]  copy block (raw text)\n"
            "  [bold]↑ / ↓[/]   input history  "
            "[bold]PgUp/PgDn[/]  scroll\n"
            "  [bold]Left click[/]  select block  "
            "[bold]Right click[/]  copy + deselect"
        )
        self._write(
            Panel(welcome_text, border_style=t.welcome.border_style, padding=t.welcome.padding),
            raw="Aar Agent TUI (Fixed) — welcome",
            kind="welcome",
        )


# ---------------------------------------------------------------------------
# Textual App
# ---------------------------------------------------------------------------


class AarFixedApp(App):
    """Full-screen Textual application for the fixed TUI mode."""

    BINDINGS = [
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

        # Build a size lookup from region config
        region_sizes: dict[str, int | None] = {}
        for region in fl.regions:
            region_sizes[region.name] = region.size

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
                ApprovalBar(),
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

    def _make_approval_callback(self):
        """Create an approval callback that shows the inline ApprovalBar."""
        app = self

        async def _approval(spec, tc) -> ApprovalResult:
            args_text = "\n".join(f"  {k}: {v}" for k, v in tc.arguments.items())
            approval_bar = app.query_one(ApprovalBar)
            done = approval_bar.show_prompt(tc.tool_name, args_text)
            await done.wait()
            return approval_bar.result

        return _approval

    def on_mount(self) -> None:
        log = self.query_one("#body-log", SelectableRichLog)
        header = self.query_one(HeaderBar)
        footer = self.query_one(FooterBar)

        # Populate header from config
        header.provider_name = self._config.provider.name
        header.model_name = self._config.provider.model

        # Apply selected block highlight style from theme
        log._selected_style = self._theme.fixed_layout.selected_block_style

        self._renderer = FixedTUIRenderer(
            log=log,
            header=header,
            footer=footer,
            verbose=self._verbose,
            theme=self._theme,
            layout=self._layout_config,
        )
        self._agent.on_event(self._renderer.render_event)

        # Wire approval callback into the agent's permission manager
        approval_cb = self._make_approval_callback()
        if hasattr(self._agent, "executor") and hasattr(self._agent.executor, "permissions"):
            self._agent.executor.permissions._approval_callback = approval_cb

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

        # Build region size lookup
        region_sizes: dict[str, int | None] = {}
        for region in fl.regions:
            region_sizes[region.name] = region.size

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
            log._selected_style = fl.selected_block_style
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
            header_size = region_sizes.get("header")
            if header_size is not None:
                header.styles.height = header_size
        except Exception:
            pass

        # Footer
        try:
            footer = self.query_one(FooterBar)
            footer.styles.background = theme.footer.background.replace("on ", "")
            footer_size = region_sizes.get("footer")
            if footer_size is not None:
                footer.styles.height = footer_size
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Key binding actions
    # ------------------------------------------------------------------

    def _scroll_speed(self) -> int:
        return self._theme.fixed_layout.scrollbar.scroll_speed

    def action_scroll_up(self) -> None:
        self.query_one("#body-log", SelectableRichLog).scroll_up(
            animate=False, duration=0, speed=self._scroll_speed()
        )

    def action_scroll_down(self) -> None:
        self.query_one("#body-log", SelectableRichLog).scroll_down(
            animate=False, duration=0, speed=self._scroll_speed()
        )

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
        """Ctrl+Y — copy the selected (or last) block's raw text to clipboard."""
        self._do_copy_selected()

    def _do_copy_selected(self) -> None:
        """Copy selected block raw text, show feedback, deselect."""
        log = self.query_one("#body-log", SelectableRichLog)
        text = log.get_selected_raw()
        if not text:
            text = log.get_last_raw()
        if text:
            self.copy_to_clipboard(text)
            log.select_block(None)  # deselect
            if self._renderer:
                self._renderer._write(
                    Text("Copied to clipboard", style=self._renderer.theme.dim_text),
                    raw="Copied to clipboard",
                    kind="system",
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
            status = Table.grid(padding=(0, 2))
            status.add_column(justify="left")
            status.add_column(justify="center")
            status.add_column(justify="right")
            status.add_row(
                f"[{t.dim_text}]Session: {self._session.session_id[:8]}...[/]",
                f"[{t.dim_text}]Steps: {self._session.step_count}[/]",
                f"[{t.dim_text}]State: {self._session.state.value}[/]",
            )
            log.write(status)
            return
        elif stripped.lower() == "/tools":
            t = self._renderer.theme
            for spec in self._agent.registry.list_tools():
                effects = ", ".join(e.value for e in spec.side_effects)
                log.write(
                    Text.from_markup(
                        f"  [bold]{spec.name}[/]  [{t.dim_text}]({effects})[/]  {spec.description}"
                    )
                )
            return
        elif stripped.lower() == "/policy":
            sc = self._config.safety
            t = Table(title="Safety Policy", show_header=True, header_style="bold")
            t.add_column("Setting", style="bold")
            t.add_column("Value")
            t.add_row("read_only", "[red]yes[/]" if sc.read_only else "[green]no[/]")
            t.add_row(
                "require_approval_for_writes",
                "[yellow]yes[/]" if sc.require_approval_for_writes else "[green]no[/]",
            )
            t.add_row(
                "require_approval_for_execute",
                "[yellow]yes[/]" if sc.require_approval_for_execute else "[green]no[/]",
            )
            t.add_row("sandbox", sc.sandbox)
            t.add_row("log_all_commands", "yes" if sc.log_all_commands else "no")
            allowed = (
                ", ".join(sc.allowed_paths) if sc.allowed_paths else "[dim]all (no whitelist)[/]"
            )
            t.add_row("allowed_paths", allowed)
            t.add_row("denied_paths", f"[dim]{len(sc.denied_paths)} patterns[/]")
            log.write(t)
            return
        elif stripped.lower() == "/clear":
            self.action_clear_screen()
            return
        elif stripped.lower().startswith("/theme"):
            parts = stripped.split(maxsplit=1)
            t = self._renderer.theme
            if len(parts) == 1:
                log.write(Text.from_markup(f"[{t.dim_text}]Current theme:[/] [bold]{t.name}[/]"))
                for tname in self._theme_registry.list_names():
                    marker = " *" if tname == t.name else ""
                    log.write(Text.from_markup(f"  [{t.dim_text}]{tname}{marker}[/]"))
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
                                style=t.error.border_style,
                            )
                        )
            return
        elif stripped.lower() == "/think":
            self._renderer.toggle_thinking()
            return
        elif stripped.lower() == "/copy":
            self._do_copy_selected()
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

        # --- Run agent (in worker so the UI event loop stays responsive) ---
        header.state = "running"
        header.refresh()
        inp.placeholder = "working..."
        inp.disabled = True
        # Re-enable auto-scroll when starting agent work
        log.auto_scroll = True

        self._run_agent_worker(content)

    def _run_agent_worker(self, content: object) -> None:
        """Launch the agent in a Textual worker so the event loop stays free."""

        async def _do_run() -> Session:
            return await self._agent.run(content, self._session)

        self.run_worker(_do_run(), exclusive=True, name="agent-run")

    def on_worker_state_changed(self, event: object) -> None:
        """Handle agent worker completion."""
        worker = getattr(event, "worker", None)
        if worker is None or getattr(worker, "name", "") != "agent-run":
            return

        from textual.worker import WorkerState

        if worker.state != WorkerState.SUCCESS:
            # Worker cancelled or errored — re-enable input
            if worker.state == WorkerState.ERROR:
                try:
                    log = self.query_one("#body-log", SelectableRichLog)
                    err_msg = str(worker.error) if worker.error else "Agent run failed"
                    log.write(Text(f"Error: {err_msg}", style=self._theme.error.border_style))
                except Exception:
                    pass
            self._restore_input()
            return

        session = worker.result
        if session is None:
            self._restore_input()
            return

        self._session = session
        header = self.query_one(HeaderBar)
        header.state = self._session.state.value
        header.session_id = self._session.session_id
        header.refresh()

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
        self._restore_input()

    def _restore_input(self) -> None:
        """Re-enable the input widget after the agent finishes."""
        try:
            inp = self.query_one("#user-input", HistoryInput)
            inp.placeholder = "> type your message..."
            inp.disabled = False
            inp.focus()
        except Exception:
            pass

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
