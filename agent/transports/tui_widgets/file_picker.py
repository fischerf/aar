"""Searchable file picker modal — opened when the user types '@' in the TUI input."""

from __future__ import annotations

from pathlib import Path

try:
    from textual.app import ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical
    from textual.screen import ModalScreen
    from textual.widgets import Input, OptionList, Static
    from textual.widgets.option_list import Option
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "The fixed TUI requires the 'textual' package. "
        'Install it with: pip install "aar-agent[tui-fixed]"'
    ) from exc

_IGNORE_DIRS = frozenset(
    {".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache", ".ruff_cache", "dist", ".tox"}
)
_MAX_COLLECT = 500
_MAX_DISPLAY = 80


class FilePickerModal(ModalScreen):
    """Full-overlay searchable file picker.

    Dismisses with the selected relative-path string, or ``None`` on cancel.
    The caller inserts ``@<path>`` into the input at cursor position.
    """

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel", show=False)]

    DEFAULT_CSS = """
    FilePickerModal {
        align: center middle;
    }
    FilePickerModal > Vertical {
        width: 80%;
        height: 70%;
        border: solid #3a4a6a;
        background: #0e121e;
    }
    #file-picker-title {
        height: 1;
        background: #1a2040;
        color: #7799cc;
        text-style: bold;
        padding: 0 1;
    }
    #file-picker-search {
        height: 3;
        border: tall #2a3a5a;
    }
    #file-picker-list {
        height: 1fr;
        scrollbar-color: #3a4a6a;
        scrollbar-background: #0e121e;
    }
    """

    def __init__(self, cwd: Path) -> None:
        super().__init__()
        self._cwd = cwd
        self._all_files: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                "  @ File  (\u2193 to list, Enter to select, Esc to cancel)",
                id="file-picker-title",
            )
            yield Input(placeholder="Type to filter files...", id="file-picker-search")
            yield OptionList(id="file-picker-list")

    def on_mount(self) -> None:
        self._all_files = self._collect_files()
        self._refresh_list("")
        self.query_one("#file-picker-search", Input).focus()

    # ------------------------------------------------------------------
    # File collection
    # ------------------------------------------------------------------

    def _collect_files(self) -> list[str]:
        results: list[str] = []
        try:
            for p in sorted(self._cwd.rglob("*")):
                if not p.is_file():
                    continue
                rel = p.relative_to(self._cwd)
                if any(part in _IGNORE_DIRS or part.startswith(".") for part in rel.parts[:-1]):
                    continue
                results.append(str(rel))
                if len(results) >= _MAX_COLLECT:
                    break
        except Exception:
            pass
        return results

    # ------------------------------------------------------------------
    # List management
    # ------------------------------------------------------------------

    def _refresh_list(self, query: str) -> None:
        lst = self.query_one("#file-picker-list", OptionList)
        lst.clear_options()
        q = query.lower()
        matches = [f for f in self._all_files if q in f.lower()] if q else self._all_files
        for path in matches[:_MAX_DISPLAY]:
            lst.add_option(Option(path))
        if matches:
            lst.highlighted = 0

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        self._refresh_list(event.value)

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        """Enter in the search box selects the currently highlighted option."""
        self._select_highlighted()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.prompt))

    def on_key(self, event: object) -> None:
        key = getattr(event, "key", "")
        search = self.query_one("#file-picker-search", Input)
        if key == "down" and search.has_focus:
            self.query_one("#file-picker-list", OptionList).focus()
            if hasattr(event, "stop"):
                event.stop()  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _select_highlighted(self) -> None:
        lst = self.query_one("#file-picker-list", OptionList)
        if lst.option_count > 0:
            idx = lst.highlighted if lst.highlighted is not None else 0
            try:
                self.dismiss(str(lst.get_option_at_index(idx).prompt))
            except Exception:
                pass

    def action_dismiss_none(self) -> None:
        self.dismiss(None)
