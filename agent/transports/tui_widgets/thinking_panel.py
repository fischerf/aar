"""ThinkingPanel — side panel widget that accumulates reasoning/thinking content."""

from __future__ import annotations

import asyncio

from rich.text import Text

try:
    from textual.containers import VerticalScroll
    from textual.widgets import Static
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "The fixed TUI requires the 'textual' package. "
        'Install it with: pip install "aar-agent[tui-fixed]"'
    ) from exc

from agent.transports.themes.models import Theme, ThinkingPanelConfig


class ThinkingPanel(VerticalScroll):
    """Scrollable side panel that streams and accumulates reasoning content.

    One instance lives for the entire session.  Each time the model starts a
    new reasoning block, ``begin_step`` is called to add a labelled separator;
    subsequent ``append`` calls stream individual tokens into the current step's
    ``Static`` widget.  When the step ends, ``finalize_step`` marks it done.

    Layout: the panel is a ``VerticalScroll`` that mounts pairs of widgets:

    - A ``Static`` separator line  (e.g. ``"── thinking (step 1) ──"``)
    - A ``Static`` content widget  (live-updated with streaming tokens)

    The caller (``FixedTUIRenderer``) owns the step counter and passes it in
    via ``begin_step`` and ``add_static_block``, so step numbers always match
    the renderer's internal counter.
    """

    DEFAULT_CSS = """
    ThinkingPanel {
        height: 100%;
        width: 40;
        overflow-y: auto;
        border-left: solid #2a2a2a;
    }
    ThinkingPanel._left_side {
        border-left: none;
        border-right: solid #2a2a2a;
    }
    ThinkingPanel Static {
        padding: 0 1;
    }
    """

    def __init__(self, theme: Theme, config: ThinkingPanelConfig) -> None:
        super().__init__()
        self._theme = theme
        self._config = config
        self._current_widget: Static | None = None  # the live-updating Static
        self._buffer: str = ""  # accumulated tokens for the current step
        self.auto_scroll: bool = True

    # ------------------------------------------------------------------
    # Public API — called by FixedTUIRenderer
    # ------------------------------------------------------------------

    def begin_step(self, step_n: int) -> None:
        """Start a new reasoning step: mount a separator and a fresh content widget."""
        cfg = self._config
        sep = Static(Text(f"── thinking (step {step_n}) ──", style=cfg.title_style))
        content = Static(Text("", style=cfg.text_style))
        self._current_widget = content
        self._buffer = ""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._mount_pair(sep, content))
        except RuntimeError:
            pass  # no event loop in unit-test context

    def append(self, token: str) -> None:
        """Append a streaming reasoning token to the current step widget."""
        if self._current_widget is None:
            return
        self._buffer += token
        try:
            self._current_widget.update(Text(self._buffer, style=self._config.text_style))
        except Exception:
            pass  # widget not yet mounted or no active app (unit-test context)

    def finalize_step(self) -> None:
        """Mark the current step as complete.

        The content widget already holds the full text — nothing needs to change
        visually.  We simply stop routing new tokens by clearing ``_current_widget``.
        """
        self._current_widget = None

    def add_static_block(self, text: str, step_n: int) -> None:
        """Add a non-streaming reasoning block (e.g. from a ``ReasoningBlock`` event).

        Args:
            text:   The full reasoning text to display.
            step_n: The step number supplied by the caller's counter.
        """
        cfg = self._config
        sep = Static(Text(f"── thinking (step {step_n}) ──", style=cfg.title_style))
        content = Static(Text(text, style=cfg.text_style))
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._mount_pair(sep, content))
        except RuntimeError:
            pass  # no event loop in unit-test context

    async def clear_log(self) -> None:
        """Remove all content and reset state."""
        await self.remove_children()
        self._current_widget = None
        self._buffer = ""

    def apply_theme(self, theme: Theme, config: ThinkingPanelConfig) -> None:
        """Update colors when the theme is switched at runtime."""
        self._theme = theme
        self._config = config
        self.styles.background = config.background
        self.styles.width = config.width
        sb = config.scrollbar
        self.styles.scrollbar_color = sb.color
        self.styles.scrollbar_color_hover = sb.color_hover
        self.styles.scrollbar_color_active = sb.color_active
        self.styles.scrollbar_background = sb.background
        self.styles.scrollbar_background_hover = sb.background_hover
        self.styles.scrollbar_background_active = sb.background_active
        self.styles.scrollbar_size_vertical = sb.size
        border_color = config.border_style
        if "_left_side" in self.classes:
            self.styles.border_right = ("solid", border_color)
            self.styles.border_left = None
        else:
            self.styles.border_left = ("solid", border_color)
            self.styles.border_right = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _mount_pair(self, sep: Static, content: Static) -> None:
        """Mount a separator + content pair and scroll to bottom."""
        await self.mount(sep)
        await self.mount(content)
        if self.auto_scroll:
            self.scroll_end(animate=False)

    def watch_virtual_size(self, new_size: object) -> None:
        """Pin to bottom when content grows, unless the user has scrolled up."""
        if self.auto_scroll:
            self.scroll_end(animate=False)

    def on_scroll(self) -> None:
        """Pause auto-scroll when user scrolls up; resume when back at bottom."""
        at_bottom = self.scroll_offset.y >= (self.virtual_size.height - self.size.height - 2)
        self.auto_scroll = at_bottom
