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
from agent.transports.themes.models import Theme


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
        self.total_cost: float = 0.0
        self.warning_active: bool = False
        self.streaming: bool = False

    def update_tokens(
        self, usage: dict[str, int], step_cost: float = 0.0, warning: bool = False
    ) -> None:
        self.input_tokens += usage.get("input_tokens", 0)
        self.output_tokens += usage.get("output_tokens", 0)
        self.total_cost += step_cost
        self.warning_active = warning

    def render(self) -> Text:  # type: ignore[override]
        h = self.theme.header
        provider = f"{self.provider_name}"
        if self.model_name:
            provider += f" / {self.model_name}"
        session = f"{self.session_id[:8]}..." if self.session_id else ""
        thinking_label = "think:on" if self.thinking_enabled else "think:off"
        tokens_style = h.tokens_warning_style if self.warning_active else h.tokens_style
        tokens_text = f"tokens: {self.input_tokens}in / {self.output_tokens}out"
        if self.total_cost > 0:
            if self.total_cost < 0.01:
                tokens_text += f" (${self.total_cost:.4f})"
            else:
                tokens_text += f" (${self.total_cost:.2f})"
        parts = [
            (provider, h.provider_style),
            ("  |  ", h.separator_style),
            (tokens_text, tokens_style),
            ("  |  ", h.separator_style),
        ]
        if session:
            parts.append((session, h.session_style))
            parts.append(("  |  ", h.separator_style))
        state_label = "streaming…" if self.streaming else self.state
        parts.append((state_label, h.state_style))
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
            ("Ctrl+S", f.step_style),
            (" send  ", f.separator_style),
            ("Ctrl+X", f.step_style),
            (" cancel  ", f.separator_style),
            ("Ctrl+T", f.step_style),
            (" theme  ", f.separator_style),
            ("Ctrl+K", f.step_style),
            (" think display  ", f.separator_style),
            ("Ctrl+L", f.step_style),
            (" clear  ", f.separator_style),
            ("Ctrl+P", f.step_style),
            (" terminal  ", f.separator_style),
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
