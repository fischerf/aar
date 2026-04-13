"""In-app log viewer for the fixed TUI.

Provides two public objects:

* ``TUI_LOG_HANDLER`` — a ``logging.Handler`` singleton that should be
  added to the root logger when the fixed TUI starts.  It buffers up to
  2 000 formatted log lines and, when a :class:`LogViewerModal` is open,
  streams new lines into the modal's ``RichLog`` widget in real-time.

* ``LogViewerModal`` — a :class:`~textual.screen.ModalScreen` that
  displays all buffered lines and any new lines emitted while it is open.
  Press **Esc** to close.
"""

from __future__ import annotations

import logging
from collections import deque

try:
    from textual.app import ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical
    from textual.screen import ModalScreen
    from textual.widgets import RichLog, Static
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "The fixed TUI requires the 'textual' package. "
        'Install it with: pip install "aar-agent[tui-fixed]"'
    ) from exc

# ---------------------------------------------------------------------------
# In-app logging handler
# ---------------------------------------------------------------------------

_LOG_FMT = logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


class _TUILogHandler(logging.Handler):
    """Buffers log records and streams them to :class:`LogViewerModal`.

    This handler is designed to be installed once as a module-level
    singleton.  The :class:`LogViewerModal` calls :meth:`attach` when it
    opens and :meth:`detach` when it closes.

    Thread safety: :meth:`emit` may be called from any thread.  When a
    widget is attached, new lines are forwarded via
    ``app.call_from_thread()`` which is safe from both OS threads and the
    asyncio event loop thread.
    """

    MAX_LINES: int = 2_000

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(_LOG_FMT)
        self._buf: deque[str] = deque(maxlen=self.MAX_LINES)
        self._widget: RichLog | None = None

    # ------------------------------------------------------------------
    # logging.Handler interface
    # ------------------------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            line = record.getMessage()

        self._buf.append(line)

        widget = self._widget
        if widget is not None:
            try:
                widget.app.call_from_thread(widget.write, line)
            except BaseException:
                pass

    # ------------------------------------------------------------------
    # Widget attachment — called by LogViewerModal
    # ------------------------------------------------------------------

    def attach(self, widget: RichLog) -> None:
        """Attach *widget* and flush all buffered lines into it."""
        self._widget = widget
        for line in self._buf:
            widget.write(line)

    def detach(self) -> None:
        """Detach the widget (called when the modal is dismissed)."""
        self._widget = None


#: Module-level singleton — import and add to the root logger in
#: :func:`~agent.transports.tui_fixed.run_tui_fixed`.
TUI_LOG_HANDLER = _TUILogHandler()


# ---------------------------------------------------------------------------
# Modal screen
# ---------------------------------------------------------------------------


class LogViewerModal(ModalScreen):
    """Full-overlay log viewer for the fixed TUI.

    Displays all log records buffered by :data:`TUI_LOG_HANDLER` and
    streams new records in real-time while the modal is open.

    Press **Esc** (or the configured ``log_viewer`` key) to dismiss.
    """

    BINDINGS = [Binding("escape", "dismiss", "Close", show=False)]

    DEFAULT_CSS = """
    LogViewerModal {
        align: center middle;
    }
    LogViewerModal > Vertical {
        width: 92%;
        height: 82%;
        border: solid #3a5a3a;
        background: #0e1a0e;
    }
    #log-viewer-title {
        height: 1;
        background: #1a2e1a;
        color: #77aa77;
        text-style: bold;
        padding: 0 1;
    }
    #log-viewer-output {
        height: 1fr;
        background: #0e1a0e;
        color: #c8dcc8;
        padding: 0 1;
        scrollbar-color: #3a5a3a;
        scrollbar-background: #0e1a0e;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("  Log Viewer  (Esc to close)", id="log-viewer-title")
            yield RichLog(
                id="log-viewer-output",
                highlight=True,
                markup=False,
                auto_scroll=True,
            )

    def on_mount(self) -> None:
        widget = self.query_one("#log-viewer-output", RichLog)
        TUI_LOG_HANDLER.attach(widget)
        widget.focus()

    def on_unmount(self) -> None:
        TUI_LOG_HANDLER.detach()
