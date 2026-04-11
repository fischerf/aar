"""Key binding configuration for the Textual fixed TUI.

Each binding is a :class:`KeyBind` object that carries both the Textual key
string (e.g. ``"ctrl+s"``) and a short label shown in the footer bar.

Users edit ``~/.aar/keybinds.json``.  Each field accepts either:

* A full object: ``{"key": "ctrl+s", "label": "send"}``
* Just a key string: ``"ctrl+s"``  (label falls back to the built-in default)

The file is created by ``aar init`` and never overwritten unless ``--force``
is passed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

log = logging.getLogger(__name__)

_USER_DIR = Path.home() / ".aar"
_USER_KEYBINDS = _USER_DIR / "keybinds.json"

# Keys known to be unreliable or undetectable in most terminal emulators.
_UNRELIABLE_KEYS: frozenset[str] = frozenset(
    {
        "ctrl+enter",
        "ctrl+return",
        "ctrl+shift+enter",
        "ctrl+shift+return",
        "alt+enter",
        "alt+return",
        "shift+enter",
        "shift+return",
    }
)


class KeyBind(BaseModel):
    """A single configurable key binding.

    Attributes:
        key:   Textual key string, e.g. ``"ctrl+s"`` or ``"pageup"``.
        label: Short label shown in the footer bar.  When empty the
               :class:`KeyBinds` consumer falls back to a built-in default.
    """

    key: str = ""
    label: str = ""

    @model_validator(mode="before")
    @classmethod
    def _coerce_str(cls, v: object) -> object:
        """Allow a bare string ``"ctrl+s"`` as shorthand for ``{"key": "ctrl+s"}``."""
        if isinstance(v, str):
            return {"key": v}
        return v

    def validate_key(self) -> list[str]:
        """Return a list of human-readable warning strings for this binding.

        An empty list means the key looks fine.  Warnings are emitted for:

        * empty key strings
        * malformed strings (e.g. ``"ctrl+"`` or ``"ctrl++s"``)
        * keys that terminals cannot distinguish from their unmodified form
          (e.g. ``"ctrl+enter"``)
        """
        warnings: list[str] = []
        k = self.key.strip()

        if not k:
            warnings.append("key string is empty")
            return warnings

        if k.endswith("+") or "++" in k:
            warnings.append(f"key '{k}' appears malformed (check for typos)")
            return warnings

        if k.lower() in _UNRELIABLE_KEYS:
            warnings.append(f"key '{k}' is not reliably detected in most terminal emulators")

        return warnings


def _kb(key: str, label: str) -> KeyBind:
    """Convenience factory used for :class:`KeyBinds` field defaults."""
    return KeyBind(key=key, label=label)


class KeyBinds(BaseModel):
    """All configurable key bindings for the fixed TUI.

    Each field is a :class:`KeyBind`.  The Pydantic coercion on
    :class:`KeyBind` means you can supply plain strings in JSON:

    .. code-block:: json

        {
            "send":   {"key": "ctrl+s", "label": "send"},
            "cancel": "ctrl+x"
        }
    """

    send: KeyBind = Field(default_factory=lambda: _kb("ctrl+s", "send"))
    cancel: KeyBind = Field(default_factory=lambda: _kb("ctrl+x", "cancel"))
    cycle_theme: KeyBind = Field(default_factory=lambda: _kb("ctrl+t", "theme"))
    toggle_thinking: KeyBind = Field(default_factory=lambda: _kb("ctrl+k", "think"))
    clear_screen: KeyBind = Field(default_factory=lambda: _kb("ctrl+l", "clear"))
    terminal: KeyBind = Field(default_factory=lambda: _kb("ctrl+p", "terminal"))
    history_prev: KeyBind = Field(default_factory=lambda: _kb("ctrl+up", "hist↑"))
    history_next: KeyBind = Field(default_factory=lambda: _kb("ctrl+down", "hist↓"))
    scroll_up: KeyBind = Field(default_factory=lambda: _kb("pageup", "pg↑"))
    scroll_down: KeyBind = Field(default_factory=lambda: _kb("pagedown", "pg↓"))

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> "KeyBinds":
        """Load keybinds from *path* (default ``~/.aar/keybinds.json``).

        Falls back to the built-in defaults if the file is absent,
        unreadable, or contains invalid JSON / schema errors.
        """
        p = path or _USER_KEYBINDS
        if not p.is_file():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return cls.model_validate(data)
        except Exception as exc:
            log.warning("keybinds: failed to load '%s' (%s) — using defaults", p, exc)
            return cls()

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_all(self) -> list[tuple[str, str]]:
        """Validate every binding and return ``(field_name, warning)`` pairs.

        An empty list means all bindings look fine.
        """
        problems: list[tuple[str, str]] = []
        for field_name in self.__class__.model_fields:
            value = getattr(self, field_name)
            if isinstance(value, KeyBind):
                for warning in value.validate_key():
                    problems.append((field_name, warning))
        return problems
