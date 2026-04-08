"""Scrollable chat body container for the Textual TUI."""

from __future__ import annotations

try:
    from textual.containers import VerticalScroll
    from textual.widgets import Static
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "The fixed TUI requires the 'textual' package. "
        'Install it with: pip install "aar-agent[tui-fixed]"'
    ) from exc


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
