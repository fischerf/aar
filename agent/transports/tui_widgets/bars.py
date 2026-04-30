"""Fixed header, footer, separator, and approval bar widgets for the Textual TUI."""

from __future__ import annotations

import asyncio

from rich.text import Text

try:
    from textual.app import ComposeResult
    from textual.containers import Horizontal
    from textual.widgets import Button, Static
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "The fixed TUI requires the 'textual' package. "
        'Install it with: pip install "aar-agent[tui-fixed]"'
    ) from exc

from agent.safety.permissions import ApprovalResult
from agent.transports.keybinds import KeyBinds
from agent.transports.themes.models import Theme


class _HeaderInfoStatic(Static):
    """Left-side info section of the header: provider / tokens / session / state.

    Receives a direct reference to its :class:`HeaderBar` owner so it never
    has to rely on ``self.parent`` (which may not be set during early renders).
    """

    DEFAULT_CSS = """
    _HeaderInfoStatic {
        width: 1fr;
        height: 1;
        content-align: left middle;
    }
    """

    def __init__(self, bar: "HeaderBar") -> None:
        super().__init__()
        self._bar = bar

    def render(self) -> Text:  # type: ignore[override]
        bar = self._bar
        h = bar.theme.header
        provider = bar.provider_name
        if bar.model_name:
            provider += f" / {bar.model_name}"
        session = f"{bar.session_id[:8]}..." if bar.session_id else ""
        thinking_label = "think:on" if bar.thinking_enabled else "think:off"
        tokens_style = h.tokens_warning_style if bar.warning_active else h.tokens_style
        tokens_text = f"tokens: {bar.input_tokens}in / {bar.output_tokens}out"
        if bar.total_cost > 0:
            if bar.total_cost < 0.01:
                tokens_text += f" (${bar.total_cost:.4f})"
            else:
                tokens_text += f" (${bar.total_cost:.2f})"
        parts: list[tuple[str, str]] = [
            (provider, h.provider_style),
            ("  |  ", h.separator_style),
            (tokens_text, tokens_style),
            ("  |  ", h.separator_style),
        ]
        if session:
            parts.append((session, h.session_style))
            parts.append(("  |  ", h.separator_style))
        state_label = "streaming\u2026" if bar.streaming else bar.state
        if bar.queue_depth > 0:
            state_label += f" | queued: {bar.queue_depth}"
        parts.append((state_label, h.state_style))
        parts.append(("  |  ", h.separator_style))
        parts.append((thinking_label, h.tokens_style))
        return Text.assemble(*parts)


class HeaderBar(Horizontal):
    """Fixed header bar: left info section + optional right kaomoji companion.

    Inherits from :class:`~textual.containers.Horizontal` so that ``1fr``
    sizing on ``_HeaderInfoStatic`` and ``width: auto`` on
    ``KaomojiCompanion`` resolve correctly — left section fills available
    space, companion is pinned to the right edge.

    All state attributes (``provider_name``, ``model_name``, ``streaming``,
    etc.) are stored directly on this widget so that callers do not need to
    know about the internal ``_HeaderInfoStatic`` child.  Call
    ``refresh_info()`` after changing attributes to trigger a repaint of the
    info section.
    """

    DEFAULT_CSS = """
    HeaderBar {
        dock: top;
        height: 1;
        padding: 0 1;
    }
    """

    def __init__(self, theme: Theme) -> None:
        super().__init__()
        self.theme = theme
        # --- display state (read by _HeaderInfoStatic.render) ---
        self.provider_name: str = ""
        self.model_name: str = ""
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.session_id: str = ""
        self.state: str = "idle"
        self.thinking_enabled: bool = True
        self.total_cost: float = 0.0
        self.warning_active: bool = False
        self.streaming: bool = False
        self.queue_depth: int = 0

    def compose(self) -> ComposeResult:
        yield _HeaderInfoStatic(self)

    def update_tokens(
        self, usage: dict[str, int], step_cost: float = 0.0, warning: bool = False
    ) -> None:
        """Accumulate token counts and refresh the info section."""
        self.input_tokens += usage.get("input_tokens", 0)
        self.output_tokens += usage.get("output_tokens", 0)
        self.total_cost += step_cost
        self.warning_active = warning

    def refresh_info(self) -> None:
        """Trigger a repaint of the left info section."""
        try:
            self.query_one(_HeaderInfoStatic).refresh()
        except Exception:
            self.refresh()


class FooterBar(Static):
    """Fixed footer showing step count, theme name, and key hints."""

    DEFAULT_CSS = """
    FooterBar {
        dock: bottom;
        height: 3;
        padding: 0 1;
    }
    """

    def __init__(self, theme: Theme, keybinds: KeyBinds | None = None) -> None:
        super().__init__()
        self.theme = theme
        self.step_count: int = 0
        self.theme_name: str = theme.name
        self._keybinds = keybinds if keybinds is not None else KeyBinds()

    def render(self) -> Text:  # type: ignore[override]
        f = self.theme.footer
        kb = self._keybinds

        def fmt_key(k: str) -> str:
            """Format a Textual key string as a display label, e.g. 'ctrl+s' → 'Ctrl+S'."""
            return "+".join(part.capitalize() for part in k.split("+"))

        def lbl(bind_label: str, fallback: str) -> str:
            """Return the configured label, or *fallback* if the label is empty."""
            return f" {bind_label if bind_label else fallback}  "

        return Text.assemble(
            (f"step: {self.step_count}", f.step_style),
            ("  |  ", f.separator_style),
            (f"theme: {self.theme_name}", f.theme_style),
            ("  |  ", f.separator_style),
            (fmt_key(kb.send.key), f.step_style),
            (lbl(kb.send.label, "send"), f.separator_style),
            (fmt_key(kb.cancel.key), f.step_style),
            (lbl(kb.cancel.label, "cancel"), f.separator_style),
            (fmt_key(kb.toggle_log_viewer.key), f.step_style),
            (lbl(kb.toggle_log_viewer.label, "logs"), f.separator_style),
            (fmt_key(kb.toggle_thinking.key), f.step_style),
            (lbl(kb.toggle_thinking.label, "think"), f.separator_style),
            (fmt_key(kb.cycle_theme.key), f.step_style),
            (lbl(kb.cycle_theme.label, "theme"), f.separator_style),
            (fmt_key(kb.clear_screen.key), f.step_style),
            (lbl(kb.clear_screen.label, "clear"), f.separator_style),
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
