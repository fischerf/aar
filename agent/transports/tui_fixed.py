"""Full-screen TUI with fixed header/footer, scrollable body, and input widget.

Built on `textual <https://textual.textualize.io>`_ for native scrollbars,
mouse wheel support, Page Up / Page Down, and a proper input line.  The
scrollable TUI (``tui.py``) remains available as the default ``aar tui`` mode;
pass ``--fixed`` to use this one.

Requires the ``tui-fixed`` optional extra::

    pip install "aar-agent[tui-fixed]"

Architecture — multiplexed streaming UI
----------------------------------------
Each phase of an LLM response gets its own widget mounted into a
``ChatBody`` (VerticalScroll):

* ``ThinkingBlock``  — plain-text stream for reasoning tokens (fast)
* ``AnswerBlock``    — batched Rich-Markdown stream for answer tokens
* ``RichBlock``      — static Rich renderable for tool calls / results / errors

This avoids the "split screen" problem of the old RichLog + separate
MarkdownStream approach: all content lives in one unified scroll container.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable

from rich.markdown import Markdown as RichMarkdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, VerticalScroll
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
    StreamChunk,
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
# Block — kept for backward compatibility with existing tests/imports
# ---------------------------------------------------------------------------


@dataclass
class _Block:
    """A single rendered block in the scrollable body."""

    raw: str  # raw text / markdown (what gets copied)
    kind: str = ""  # "assistant", "tool_call", "tool_result", "reasoning", "error", etc.
    line_start: int = 0
    line_count: int = 0


# ---------------------------------------------------------------------------
# Fixed widgets: header, footer, separator
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
            ("Shift+Enter", f.step_style),
            (" newline  ", f.separator_style),
            ("Ctrl+C", f.step_style),
            (" cancel  ", f.separator_style),
            ("Ctrl+T", f.step_style),
            (" theme  ", f.separator_style),
            ("Ctrl+K", f.step_style),
            (" think display  ", f.separator_style),
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

    def __init__(self, style: str = "dim", character: str = "─") -> None:
        super().__init__()
        self._style = style
        self._character = character

    def render(self) -> Text:  # type: ignore[override]
        return Text(self._character * self.size.width, style=self._style)


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
            yield Button("(y) Yes", id="approval-yes", flat=True, variant="success")
            yield Button("(n) No", id="approval-no", flat=True, variant="error")
            yield Button("(a) Always", id="approval-always", flat=True, variant="warning")

    def show_prompt(self, tool_name: str, args_text: str) -> asyncio.Event:
        """Display an approval prompt. Returns an asyncio.Event that is set when answered."""
        self._result_event = asyncio.Event()
        self._result = ApprovalResult.DENIED
        text_widget = self.query_one("#approval-text", Static)
        text_widget.update(
            f"[bold red]Approval Required[/]\n[bold]{tool_name}[/]\n{args_text}\n[bold]Allow?[/]"
        )
        self.add_class("visible")
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
        self._border_type: str = "tall"
        self._border_color: str = "#444444"
        self._border_color_focus: str = "#888888"

    def on_focus(self) -> None:
        """Switch to the focused border color."""
        self.styles.border = (self._border_type, self._border_color_focus)

    def on_blur(self) -> None:
        """Switch back to the unfocused border color."""
        self.styles.border = (self._border_type, self._border_color)

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
        """Intercept up/down keys for history and shift+enter for newlines."""
        key = getattr(event, "key", "")
        if key == "shift+enter":
            pos = self.cursor_position
            self.value = self.value[:pos] + "\n" + self.value[pos:]
            self.cursor_position = pos + 1
            if hasattr(event, "prevent_default"):
                event.prevent_default()  # type: ignore[union-attr]
            if hasattr(event, "stop"):
                event.stop()  # type: ignore[union-attr]
        elif key == "up":
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
# SelectableRichLog — kept for backward compatibility with existing tests
# ---------------------------------------------------------------------------


class SelectableRichLog(RichLog):
    """RichLog that tracks rendered blocks and supports click-to-select + copy.

    Kept for backward compatibility. The live app now uses ChatBody instead.
    """

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        self._blocks: list[_Block] = []
        self._selected_block: int | None = None
        self._total_lines: int = 0
        self._selected_style: str = "on #2a2a3a"

    def write_block(
        self,
        content: object,
        raw: str,
        kind: str = "",
        **kwargs,  # noqa: ANN003
    ) -> "SelectableRichLog":
        """Write a renderable to the log and track its raw text for copy."""
        line_start = self._total_lines
        result = self.write(content, **kwargs)
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
    ) -> "SelectableRichLog":
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

    def select_block(self, idx: int | None) -> None:
        self._selected_block = idx
        self.refresh()

    def get_selected_raw(self) -> str:
        if self._selected_block is not None and 0 <= self._selected_block < len(self._blocks):
            return self._blocks[self._selected_block].raw
        return ""

    def get_last_raw(self) -> str:
        if self._blocks:
            return self._blocks[-1].raw
        return ""

    def get_all_text(self) -> str:
        return "\n\n".join(b.raw for b in self._blocks)


# ---------------------------------------------------------------------------
# Multiplexed streaming widgets (new architecture)
# ---------------------------------------------------------------------------


class ThinkingBlock(Static):
    """Live-streaming reasoning block — plain text for fast token-by-token updates."""

    DEFAULT_CSS = """
    ThinkingBlock {
        padding: 0 1;
    }
    """

    def __init__(self, theme: Theme) -> None:
        super().__init__()
        self._theme = theme
        self._buffer = ""
        self.raw = ""
        self.kind = "reasoning"
        self._selected = False

    def append(self, token: str) -> None:
        self._buffer += token
        self.raw = self._buffer
        t = self._theme
        self.update(
            Panel(
                Text(self._buffer, style=f"italic {t.reasoning.border_style}"),
                title=f"[{t.reasoning.title_style}]Thinking...[/]",
                border_style=t.reasoning.border_style,
                padding=t.reasoning.padding,
            )
        )

    def finalize(self) -> None:
        """Trim to 500 chars and remove the '...' from the title."""
        text = self._buffer
        if len(text) > 500:
            text = text[:500] + "..."
        self.raw = self._buffer
        t = self._theme
        self.update(
            Panel(
                Text(text, style=f"italic {t.reasoning.border_style}"),
                title=f"[{t.reasoning.title_style}]Thinking[/]",
                border_style=t.reasoning.border_style,
                padding=t.reasoning.padding,
            )
        )

    async def on_click(self, event: Click) -> None:
        self._selected = not self._selected
        if event.button == 3 and hasattr(self.app, "_do_copy_selected"):
            self.app._do_copy_selected()  # type: ignore[attr-defined]


class AnswerBlock(Static):
    """Live-streaming answer block — batched Rich Markdown for performance."""

    DEFAULT_CSS = """
    AnswerBlock {
        padding: 0 1;
    }
    """

    _BATCH = 15  # render every N tokens

    def __init__(self, theme: Theme) -> None:
        super().__init__()
        self._theme = theme
        self._buffer = ""
        self._token_count = 0
        self.raw = ""
        self.kind = "assistant"
        self._selected = False

    def append(self, token: str) -> None:
        self._buffer += token
        self._token_count += 1
        self.raw = self._buffer
        if self._token_count % self._BATCH == 0:
            self._do_render()

    def _do_render(self) -> None:
        t = self._theme
        self.update(
            Panel(
                RichMarkdown(self._buffer),
                title=f"[{t.assistant.title_style}]Assistant[/]",
                border_style=t.assistant.border_style,
                padding=t.assistant.padding,
            )
        )

    def finalize(self, content: str | None = None) -> None:
        """Final render — use authoritative full content if provided."""
        if content is not None:
            self._buffer = content
        self.raw = self._buffer
        self._do_render()

    async def on_click(self, event: Click) -> None:
        self._selected = not self._selected
        if event.button == 3 and hasattr(self.app, "_do_copy_selected"):
            self.app._do_copy_selected()  # type: ignore[attr-defined]


class RichBlock(Static):
    """Static block for any Rich renderable: tool calls, errors, system messages, etc."""

    DEFAULT_CSS = """
    RichBlock {
        padding: 0 1;
    }
    RichBlock.selected {
        opacity: 0.85;
    }
    """

    def __init__(self, content: object, raw: str = "", kind: str = "") -> None:
        super().__init__()
        self.raw = raw
        self.kind = kind
        self._selected = False
        self.update(content)

    async def on_click(self, event: Click) -> None:
        if self._selected:
            self._selected = False
            self.remove_class("selected")
        else:
            # Deselect all siblings
            if self.parent:
                for sibling in self.parent.query("RichBlock.selected"):
                    sibling._selected = False  # type: ignore[attr-defined]
                    sibling.remove_class("selected")
            self._selected = True
            self.add_class("selected")
        if event.button == 3 and hasattr(self.app, "_do_copy_selected"):
            self.app._do_copy_selected()  # type: ignore[attr-defined]


class ChatBody(VerticalScroll):
    """Main scrollable chat area — the unified container for all content blocks."""

    DEFAULT_CSS = """
    ChatBody {
        min-height: 4;
    }
    """

    def __init__(self, **kwargs) -> None:  # noqa: ANN003
        super().__init__(**kwargs)
        self.auto_scroll: bool = True

    async def _mount_block(self, widget: Static) -> None:
        """Mount a content block and scroll to bottom if auto-scroll is on."""
        await self.mount(widget)
        if self.auto_scroll:
            self.scroll_end(animate=False)

    def watch_virtual_size(self, new_size) -> None:  # noqa: ANN001
        """Pin to bottom whenever content grows, unless the user has scrolled up."""
        if self.auto_scroll:
            self.scroll_end(animate=False)

    def on_scroll(self) -> None:
        """Pause auto-scroll when user scrolls up; resume when back at bottom."""
        at_bottom = self.scroll_offset.y >= (self.virtual_size.height - self.size.height - 2)
        self.auto_scroll = at_bottom

    def get_selected_raw(self) -> str:
        """Return raw text of the currently selected block."""
        for block in self.query("RichBlock.selected"):
            raw = getattr(block, "raw", "")
            if raw:
                return raw
        for block in self.query("ThinkingBlock, AnswerBlock"):
            if getattr(block, "_selected", False):
                raw = getattr(block, "raw", "")
                if raw:
                    return raw
        return ""

    def get_last_raw(self) -> str:
        """Return raw text of the most recent content block."""
        blocks = list(self.query("ThinkingBlock, AnswerBlock, RichBlock"))
        for block in reversed(blocks):
            raw = getattr(block, "raw", "")
            if raw:
                return raw
        return ""

    def get_all_text(self) -> str:
        """Concatenate raw text from all content blocks."""
        blocks = list(self.query("ThinkingBlock, AnswerBlock, RichBlock"))
        return "\n\n".join(getattr(b, "raw", "") for b in blocks if getattr(b, "raw", ""))


# ---------------------------------------------------------------------------
# FixedTUIRenderer — routes agent events to the appropriate widgets
# ---------------------------------------------------------------------------


class FixedTUIRenderer:
    """Renders agent events into the chat UI.

    Supports two modes:
    - *App mode*: ``chat_body`` is set — blocks are mounted asynchronously.
    - *Test mode*: ``log`` is set — content is written synchronously to a
      ``SelectableRichLog`` (or any duck-typed stand-in).
    """

    def __init__(
        self,
        header: HeaderBar,
        footer: FooterBar,
        log: "RichLog | SelectableRichLog | None" = None,
        chat_body: ChatBody | None = None,
        verbose: bool = False,
        theme: Theme | None = None,
        layout: LayoutConfig | None = None,
    ) -> None:
        self._log = log
        self._chat_body = chat_body
        self._header = header
        self._footer = footer
        self._verbose = verbose
        self._usage_total: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self._step_count = 0
        self.theme = theme or DEFAULT_THEME
        self.layout = layout or LayoutConfig()
        self._extension_panels: dict[str, Callable] = {}
        self._thinking_visible = True
        self._streaming_active = False
        self._stream_in_reasoning = False
        # Live streaming widget references (app mode only)
        self._current_thinking: ThinkingBlock | None = None
        self._current_answer: AnswerBlock | None = None

    def _write(self, content: object, raw: str = "", kind: str = "") -> None:
        """Write a content block — sync (test mode) or async mount (app mode)."""
        if self._log is not None:
            if hasattr(self._log, "write_block") and raw:
                self._log.write_block(content, raw=raw, kind=kind)
            else:
                self._log.write(content)
        if self._chat_body is not None:
            block = RichBlock(content, raw=raw, kind=kind)
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._chat_body._mount_block(block))
            except RuntimeError:
                pass  # no running loop (unit-test context without Textual)

    def _mount_streaming(self, widget: Static) -> None:
        """Mount a ThinkingBlock or AnswerBlock to the chat body."""
        if self._chat_body is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._chat_body._mount_block(widget))
            except RuntimeError:
                pass

    # ------------------------------------------------------------------
    # Theme switching
    # ------------------------------------------------------------------

    def set_theme(self, theme: Theme, app: "AarFixedApp") -> None:
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

    def cycle_theme(self, registry: ThemeRegistry, app: "AarFixedApp") -> None:
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
    # Event rendering
    # ------------------------------------------------------------------

    def render_event(self, event: Event) -> None:  # noqa: C901
        """Render a single agent event."""
        t = self.theme

        # --- Streaming tokens -------------------------------------------------
        if isinstance(event, StreamChunk):
            if event.reasoning_text and self._thinking_visible:
                if not self._streaming_active:
                    self._streaming_active = True
                    self._stream_in_reasoning = True
                if self._current_thinking is None:
                    self._current_thinking = ThinkingBlock(self.theme)
                    self._mount_streaming(self._current_thinking)
                self._current_thinking.append(event.reasoning_text)

            if event.text:
                if not self._streaming_active:
                    self._streaming_active = True
                if self._stream_in_reasoning:
                    self._stream_in_reasoning = False
                    # Finalize thinking title so it no longer shows "..."
                    if self._current_thinking is not None:
                        self._current_thinking.finalize()
                if self._current_answer is None:
                    self._current_answer = AnswerBlock(self.theme)
                    self._mount_streaming(self._current_answer)
                self._current_answer.append(event.text)

            if event.finished:
                self._stream_in_reasoning = False
                if self._current_thinking is not None:
                    self._current_thinking.finalize()
                # _streaming_active stays True until AssistantMessage arrives
                # so we know to finalize (not re-create) the answer block.
            return

        # --- Final assistant message ------------------------------------------
        if isinstance(event, AssistantMessage) and event.content:
            if self._streaming_active:
                # Streaming completed: update blocks with authoritative content.
                self._streaming_active = False
                self._stream_in_reasoning = False
                if self._current_answer is not None:
                    self._current_answer.finalize(event.content)
                    self._current_answer = None
                if self._current_thinking is not None:
                    self._current_thinking.finalize()
                    self._current_thinking = None
            else:
                # No streaming (e.g. non-streaming provider): write static block.
                if not self.layout.assistant.visible:
                    return
                self._write(
                    Panel(
                        RichMarkdown(event.content),
                        title=f"[{t.assistant.title_style}]Assistant[/]",
                        border_style=t.assistant.border_style,
                        padding=t.assistant.padding,
                    ),
                    raw=event.content,
                    kind="assistant",
                )
            return

        # --- Tool call --------------------------------------------------------
        if isinstance(event, ToolCall):
            self._streaming_active = False
            self._stream_in_reasoning = False
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

        # --- Tool result ------------------------------------------------------
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

        # --- Reasoning block (non-streaming) ----------------------------------
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

        # --- Error ------------------------------------------------------------
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

        # --- Provider metadata -----------------------------------------------
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
            "[bold]Aar Agent TUI (Textual)[/]\n\n"
            "Type your message and press Enter.\n"
            "Attach files with @path (e.g. @photo.jpg @audio.wav)\n"
            "Commands: [bold]/quit[/] [bold]/status[/] [bold]/tools[/] "
            "[bold]/policy[/] [bold]/theme[/] [bold]/clear[/]\n\n"
            "[bold]Shortcuts:[/]\n"
            "  [bold]Ctrl+T[/]  cycle theme    "
            "[bold]Ctrl+K[/]  toggle thinking display\n"
            "  [bold]Ctrl+L[/]  clear screen   "
            "[bold]Ctrl+Y[/]  copy block (raw text)\n"
            "  [bold]↑ / ↓[/]   input history  "
            "[bold]PgUp/PgDn[/]  scroll\n"
            "  [bold]Left click[/]  select block  "
            "[bold]Right click[/]  copy + deselect"
        )
        self._write(
            Panel(welcome_text, border_style=t.welcome.border_style, padding=t.welcome.padding),
            raw="Aar Agent TUI (Textual) — welcome",
            kind="welcome",
        )


# ---------------------------------------------------------------------------
# Textual App
# ---------------------------------------------------------------------------


class AarFixedApp(App):
    """Full-screen Textual application for the Textual TUI mode."""

    BINDINGS = [
        Binding("pageup", "scroll_up", "Page Up", show=False),
        Binding("pagedown", "scroll_down", "Page Down", show=False),
        Binding("ctrl+c", "cancel_agent", "Cancel agent", show=False, priority=True),
        Binding("ctrl+t", "cycle_theme", "Cycle theme", show=False),
        Binding("ctrl+k", "toggle_thinking", "Toggle thinking", show=False, priority=True),
        Binding("ctrl+l", "clear_screen", "Clear screen", show=False),
        Binding("ctrl+y", "copy_block", "Copy selected block", show=False),
    ]

    CSS = """
    Screen {
        layout: vertical;
    }
    #chat-body {
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
        self._cancel_event: asyncio.Event | None = None

    # ------------------------------------------------------------------
    # Compose the widget tree from theme layout config
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        fl = self._theme.fixed_layout

        region_sizes: dict[str, int | None] = {}
        for region in fl.regions:
            region_sizes[region.name] = region.size

        widget_map: dict[str, Callable[[], list]] = {
            "header": lambda: [
                HeaderBar(self._theme),
                SeparatorBar(
                    self._theme.header.separator.style,
                    self._theme.header.separator.character,
                ),
            ],
            "body": lambda: [
                ChatBody(id="chat-body"),
            ],
            "input": lambda: [
                ApprovalBar(),
                SeparatorBar(
                    self._theme.footer.separator.style,
                    self._theme.footer.separator.character,
                ),
                HistoryInput(placeholder="> type your message...", id="user-input"),
            ],
            "footer": lambda: [
                SeparatorBar(
                    self._theme.footer.separator.style,
                    self._theme.footer.separator.character,
                ),
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
            args_text = "\n".join(f"  {k}: {v}" for k, v in tc.arguments.items() if k != "content")
            approval_bar = app.query_one(ApprovalBar)
            done = approval_bar.show_prompt(tc.tool_name, args_text)
            await done.wait()
            return approval_bar.result

        return _approval

    def on_mount(self) -> None:
        chat_body = self.query_one("#chat-body", ChatBody)
        header = self.query_one(HeaderBar)
        footer = self.query_one(FooterBar)

        header.provider_name = self._config.provider.name
        header.model_name = self._config.provider.model

        # Apply selected block highlight style from theme to ChatBody CSS
        fl = self._theme.fixed_layout
        chat_body.styles.background = fl.body_background

        self._renderer = FixedTUIRenderer(
            chat_body=chat_body,
            header=header,
            footer=footer,
            verbose=self._verbose,
            theme=self._theme,
            layout=self._layout_config,
        )

        self._agent.on_event(self._renderer.render_event)

        approval_cb = self._make_approval_callback()
        if hasattr(self._agent, "executor") and hasattr(self._agent.executor, "permissions"):
            self._agent.executor.permissions._approval_callback = approval_cb

        self.apply_theme(self._theme)

        if self._session_id:
            try:
                self._session = self._store.load(self._session_id)
                header.session_id = self._session.session_id
                header.refresh()
                loop = asyncio.get_running_loop()
                loop.create_task(
                    chat_body._mount_block(
                        RichBlock(
                            Text(
                                f"Resumed session {self._session_id}",
                                style=self._theme.dim_text,
                            ),
                            raw=f"Resumed session {self._session_id}",
                            kind="system",
                        )
                    )
                )
            except FileNotFoundError:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    chat_body._mount_block(
                        RichBlock(
                            Text(
                                f"Session {self._session_id} not found",
                                style=self._theme.error.border_style,
                            ),
                            raw=f"Session {self._session_id} not found",
                            kind="error",
                        )
                    )
                )

        self._renderer.render_welcome()

        self.query_one("#user-input", HistoryInput).focus()

    def apply_theme(self, theme: Theme) -> None:
        """Apply theme colors to Textual widget styles."""
        self._theme = theme
        fl = theme.fixed_layout
        sb = fl.scrollbar

        region_sizes: dict[str, int | None] = {}
        for region in fl.regions:
            region_sizes[region.name] = region.size

        # Chat body
        try:
            chat_body = self.query_one("#chat-body", ChatBody)
            chat_body.styles.background = fl.body_background
            chat_body.styles.scrollbar_color = sb.color
            chat_body.styles.scrollbar_color_hover = sb.color_hover
            chat_body.styles.scrollbar_color_active = sb.color_active
            chat_body.styles.scrollbar_background = sb.background
            chat_body.styles.scrollbar_background_hover = sb.background_hover
            chat_body.styles.scrollbar_background_active = sb.background_active
            chat_body.styles.scrollbar_size_vertical = sb.size
        except Exception:
            pass

        # Input
        try:
            inp = self.query_one("#user-input", HistoryInput)
            inp.styles.background = fl.input_background
            ifield = fl.input_field
            inp.styles.border = (ifield.border_type, ifield.border_color)
            inp.styles.color = ifield.text_color
            inp._border_color = ifield.border_color
            inp._border_color_focus = ifield.border_color_focus
            inp._border_type = ifield.border_type
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

        # Separator bars
        try:
            separators = self.query(SeparatorBar)
            for i, sep in enumerate(separators):
                if i == 0:
                    sep._style = theme.header.separator.style
                    sep._character = theme.header.separator.character
                else:
                    sep._style = theme.footer.separator.style
                    sep._character = theme.footer.separator.character
                sep.refresh()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Key binding actions
    # ------------------------------------------------------------------

    def _scroll_speed(self) -> int:
        return self._theme.fixed_layout.scrollbar.scroll_speed

    def action_scroll_up(self) -> None:
        self.query_one("#chat-body", ChatBody).scroll_up(
            animate=False, duration=0, speed=self._scroll_speed()
        )

    def action_scroll_down(self) -> None:
        self.query_one("#chat-body", ChatBody).scroll_down(
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

    async def action_clear_screen(self) -> None:
        """Ctrl+L — clear the chat body and reset counters."""
        if not self._renderer:
            return
        chat_body = self.query_one("#chat-body", ChatBody)
        header = self.query_one(HeaderBar)
        footer = self.query_one(FooterBar)
        await chat_body.remove_children()
        chat_body.auto_scroll = True
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
        self._renderer._streaming_active = False
        self._renderer._stream_in_reasoning = False
        self._renderer._current_thinking = None
        self._renderer._current_answer = None
        self._renderer.render_welcome()

    async def action_cancel_agent(self) -> None:
        """Ctrl+C — cancel the running agent."""
        if self._cancel_event is not None:
            self._cancel_event.set()
        for worker in self.workers:
            if getattr(worker, "name", "") == "agent-run" and worker.is_running:
                worker.cancel()
                if self._renderer:
                    # Finalize any live streaming blocks
                    if self._renderer._current_thinking is not None:
                        self._renderer._current_thinking.finalize()
                        self._renderer._current_thinking = None
                    if self._renderer._current_answer is not None:
                        self._renderer._current_answer.finalize()
                        self._renderer._current_answer = None
                    self._renderer._streaming_active = False
                    self._renderer._write(
                        Text("Cancelled", style=self._renderer.theme.error.border_style),
                        raw="Cancelled",
                        kind="system",
                    )
                try:
                    header = self.query_one(HeaderBar)
                    header.state = "cancelled"
                    header.refresh()
                except Exception:
                    pass
                self._restore_input()
                break

    def action_copy_block(self) -> None:
        """Ctrl+Y — copy the selected (or last) block's raw text to clipboard."""
        self._do_copy_selected()

    def _do_copy_selected(self) -> None:
        """Copy selected block raw text, show feedback, deselect."""
        try:
            chat_body = self.query_one("#chat-body", ChatBody)
        except Exception:
            return
        text = chat_body.get_selected_raw()
        if not text:
            text = chat_body.get_last_raw()
        if text:
            self.copy_to_clipboard(text)
            # Deselect all selected blocks
            for block in chat_body.query("RichBlock.selected"):
                block._selected = False  # type: ignore[attr-defined]
                block.remove_class("selected")
            if self._renderer:
                self._renderer._write(
                    Text("Copied to clipboard", style=self._renderer.theme.dim_text),
                    raw="Copied to clipboard",
                    kind="system",
                )

    # ------------------------------------------------------------------
    # Input handling
    # ------------------------------------------------------------------

    async def on_input_submitted(self, event: "Input.Submitted") -> None:
        """Handle user input from the Input widget."""
        user_input = event.value
        inp = self.query_one("#user-input", HistoryInput)
        inp.value = ""
        stripped = user_input.strip()
        if not stripped:
            return

        inp.add_to_history(stripped)

        assert self._renderer is not None
        chat_body = self.query_one("#chat-body", ChatBody)
        header = self.query_one(HeaderBar)
        t = self._renderer.theme

        async def _write(content: object, raw: str = "", kind: str = "") -> None:
            await chat_body._mount_block(RichBlock(content, raw=raw, kind=kind))

        # Echo input
        await _write(Text(f"  > {stripped}", style=self._renderer.theme.prompt_style))

        # --- TUI commands ------------------------------------------------
        if stripped.lower() in {"/quit", "/exit", "/q"}:
            self.exit()
            return
        elif stripped.lower() == "/status":
            if not self._session:
                await _write(f"[{t.dim_text}]No active session.[/]")
            else:
                status = Table.grid(padding=(0, 2))
                status.add_column(justify="left")
                status.add_column(justify="center")
                status.add_column(justify="right")
                status.add_row(
                    f"[{t.dim_text}]Session: {self._session.session_id[:8]}...[/]",
                    f"[{t.dim_text}]Steps: {self._session.step_count}[/]",
                    f"[{t.dim_text}]State: {self._session.state.value}[/]",
                )
                await _write(status)
            return
        elif stripped.lower() == "/tools":
            for spec in self._agent.registry.list_tools():
                effects = ", ".join(e.value for e in spec.side_effects)
                await _write(
                    Text.from_markup(
                        f"  [bold]{spec.name}[/]  [{t.dim_text}]({effects})[/]  {spec.description}"
                    )
                )
            return
        elif stripped.lower() == "/policy":
            sc = self._config.safety
            tbl = Table(title="Safety Policy", show_header=True, header_style="bold")
            tbl.add_column("Setting", style="bold")
            tbl.add_column("Value")
            tbl.add_row("read_only", "[red]yes[/]" if sc.read_only else "[green]no[/]")
            tbl.add_row(
                "require_approval_for_writes",
                "[yellow]yes[/]" if sc.require_approval_for_writes else "[green]no[/]",
            )
            tbl.add_row(
                "require_approval_for_execute",
                "[yellow]yes[/]" if sc.require_approval_for_execute else "[green]no[/]",
            )
            tbl.add_row("sandbox", sc.sandbox)
            tbl.add_row("log_all_commands", "yes" if sc.log_all_commands else "no")
            allowed = (
                ", ".join(sc.allowed_paths) if sc.allowed_paths else "[dim]all (no whitelist)[/]"
            )
            tbl.add_row("allowed_paths", allowed)
            tbl.add_row("denied_paths", f"[dim]{len(sc.denied_paths)} patterns[/]")
            await _write(tbl)
            return
        elif stripped.lower() == "/clear":
            await self.action_clear_screen()
            return
        elif stripped.lower().startswith("/theme"):
            parts = stripped.split(maxsplit=1)
            if len(parts) == 1:
                await _write(Text.from_markup(f"[{t.dim_text}]Current theme:[/] [bold]{t.name}[/]"))
                for tname in self._theme_registry.list_names():
                    marker = " *" if tname == t.name else ""
                    await _write(Text.from_markup(f"  [{t.dim_text}]{tname}{marker}[/]"))
            else:
                arg = parts[1].strip()
                if arg == "next":
                    self._renderer.cycle_theme(self._theme_registry, self)
                else:
                    try:
                        self._renderer.set_theme(self._theme_registry.get(arg), self)
                    except KeyError:
                        await _write(Text(f"Unknown theme: {arg}", style=t.error.border_style))
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
                    await _write(Text("  Attached: image", style=self._renderer.theme.dim_text))
                elif isinstance(block, AudioBlock):
                    await _write(Text("  Attached: audio", style=self._renderer.theme.dim_text))
                    has_audio = True
            if has_audio and not self._agent.provider.supports_audio:
                await _write(
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
        chat_body.auto_scroll = True

        self._run_agent_worker(content)

    def _run_agent_worker(self, content: object) -> None:
        """Launch the agent in a Textual worker so the event loop stays free."""
        cancel_event = asyncio.Event()
        self._cancel_event = cancel_event

        async def _do_run() -> Session:
            return await self._agent.run(content, self._session, cancel_event=cancel_event)

        self.run_worker(_do_run(), exclusive=True, name="agent-run")

    async def on_worker_state_changed(self, event: object) -> None:
        """Handle agent worker completion."""
        worker = getattr(event, "worker", None)
        if worker is None or getattr(worker, "name", "") != "agent-run":
            return

        from textual.worker import WorkerState

        if worker.state != WorkerState.SUCCESS:
            if worker.state == WorkerState.ERROR:
                try:
                    chat_body = self.query_one("#chat-body", ChatBody)
                    err_msg = str(worker.error) if worker.error else "Agent run failed"
                    await chat_body._mount_block(
                        RichBlock(
                            Text(f"Error: {err_msg}", style=self._theme.error.border_style),
                            raw=err_msg,
                            kind="error",
                        )
                    )
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
