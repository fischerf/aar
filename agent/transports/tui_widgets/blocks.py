"""Content block widgets for the Textual TUI: thinking, answer, rich, and selectable log."""

from __future__ import annotations

from dataclasses import dataclass

from rich.markdown import Markdown as RichMarkdown
from rich.panel import Panel
from rich.text import Text

try:
    from textual.events import Click
    from textual.widgets import RichLog, Static
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "The fixed TUI requires the 'textual' package. "
        'Install it with: pip install "aar-agent[tui-fixed]"'
    ) from exc

from agent.transports.themes.models import Theme

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
# Multiplexed streaming widgets
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
