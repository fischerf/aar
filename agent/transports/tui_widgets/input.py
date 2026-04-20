"""Input widget with command history for the Textual TUI."""

from __future__ import annotations

import re

_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[^a-zA-Z]*[a-zA-Z]|[^\[])")

try:
    from textual.message import Message
    from textual.widgets import Input, TextArea
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "The fixed TUI requires the 'textual' package. "
        'Install it with: pip install "aar-agent[tui-fixed]"'
    ) from exc


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


class HistoryTextArea(TextArea):
    """Multi-line text area with history navigation via ctrl+up/down and ctrl+enter to submit."""

    class Submitted(Message):
        """Posted when the user submits the text area content via ctrl+enter."""

        def __init__(self, textarea: "HistoryTextArea", value: str) -> None:
            super().__init__()
            self.textarea = textarea
            self.value = value

    class AtTriggered(Message):
        """Posted when the user types '@' — signals the app to open the file picker."""

        def __init__(self, textarea: "HistoryTextArea") -> None:
            super().__init__()
            self.textarea = textarea

    def __init__(
        self,
        *args,
        send_key: str = "ctrl+s",
        history_prev_key: str = "ctrl+up",
        history_next_key: str = "ctrl+down",
        **kwargs,
    ) -> None:
        self._send_key = send_key
        self._history_prev_key = history_prev_key
        self._history_next_key = history_next_key
        self._history: list[str] = []
        self._history_index: int = -1
        self._draft: str = ""
        self._border_type: str = "tall"
        self._border_color: str = "#444444"
        self._border_color_focus: str = "#888888"
        super().__init__(*args, **kwargs)

    def on_focus(self) -> None:
        """Switch to the focused border color."""
        self.styles.border = (self._border_type, self._border_color_focus)

    def on_blur(self) -> None:
        """Switch back to the unfocused border color."""
        self.styles.border = (self._border_type, self._border_color)

    def action_submit_message(self) -> None:
        """Post a Submitted message if the text area is non-empty."""
        if self.text.strip():
            self.post_message(self.Submitted(self, self.text))

    def clear(self) -> None:
        """Clear the text area content."""
        self.text = ""

    def add_to_history(self, text: str) -> None:
        """Add an entry to the history (deduplicates consecutive identical entries)."""
        text = text.strip()
        if text and (not self._history or self._history[-1] != text):
            self._history.append(text)
        self._history_index = -1
        self._draft = ""

    async def _on_key(self, event: object) -> None:
        """Handle send key, @ file picker trigger, and history navigation."""
        key = getattr(event, "key", "")
        if getattr(event, "character", None) == "@":
            self.post_message(self.AtTriggered(self))
            if hasattr(event, "prevent_default"):
                event.prevent_default()  # type: ignore[union-attr]
            if hasattr(event, "stop"):
                event.stop()  # type: ignore[union-attr]
        elif key == self._send_key:
            self.action_submit_message()
            if hasattr(event, "prevent_default"):
                event.prevent_default()  # type: ignore[union-attr]
            if hasattr(event, "stop"):
                event.stop()  # type: ignore[union-attr]
        elif key == self._history_prev_key:
            if not self._history:
                return
            if self._history_index == -1:
                self._draft = self.text
                self._history_index = len(self._history) - 1
            elif self._history_index > 0:
                self._history_index -= 1
            self.text = self._history[self._history_index]
            if hasattr(event, "prevent_default"):
                event.prevent_default()  # type: ignore[union-attr]
            if hasattr(event, "stop"):
                event.stop()  # type: ignore[union-attr]
        elif key == self._history_next_key:
            if self._history_index == -1:
                return
            if self._history_index < len(self._history) - 1:
                self._history_index += 1
                self.text = self._history[self._history_index]
            else:
                self._history_index = -1
                self.text = self._draft
            if hasattr(event, "prevent_default"):
                event.prevent_default()  # type: ignore[union-attr]
            if hasattr(event, "stop"):
                event.stop()  # type: ignore[union-attr]

    def on_paste(self, event: object) -> None:
        """Strip ANSI escape sequences that arrive via paste (e.g. mouse/focus tracking on Windows)."""
        text = getattr(event, "text", None)
        if text is None:
            return
        clean = _ANSI_ESCAPE_RE.sub("", text)
        if clean == text:
            return
        if hasattr(event, "prevent_default"):
            event.prevent_default()  # type: ignore[union-attr]
        self.insert(clean)
