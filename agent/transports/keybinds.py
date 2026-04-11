"""Key binding configuration for the Textual fixed TUI.

All bindings are hardcoded here.  To change a key or its footer label,
edit the :class:`KeyBinds` dataclass fields directly — no JSON file needed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KeyBind:
    """A single key binding: a Textual key string and a short footer label."""

    key: str
    label: str


@dataclass(frozen=True)
class KeyBinds:
    """All keyboard shortcuts for the fixed TUI.

    Every field is a :class:`KeyBind`.  The ``key`` value is a Textual key
    string (e.g. ``"ctrl+s"``, ``"pageup"``).  The ``label`` is displayed in
    the footer bar.

    To remap a shortcut, change the ``key`` value here.
    :class:`~agent.transports.tui_fixed.AarFixedApp` derives its ``BINDINGS``
    from the module-level ``_KB`` instance automatically — no second edit needed.
    """

    # Input
    send: KeyBind = KeyBind("ctrl+s", "send")
    history_prev: KeyBind = KeyBind("ctrl+up", "hist↑")
    history_next: KeyBind = KeyBind("ctrl+down", "hist↓")

    # Agent control
    cancel: KeyBind = KeyBind("ctrl+x", "cancel")

    # Navigation
    scroll_up: KeyBind = KeyBind("pageup", "pg↑")
    scroll_down: KeyBind = KeyBind("pagedown", "pg↓")

    # View toggles
    cycle_theme: KeyBind = KeyBind("ctrl+t", "theme")
    toggle_thinking: KeyBind = KeyBind("ctrl+k", "think")
    clear_screen: KeyBind = KeyBind("ctrl+l", "clear")

    # Modals
    toggle_log_viewer: KeyBind = KeyBind("ctrl+g", "logs")
