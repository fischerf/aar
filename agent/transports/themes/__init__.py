"""Theme registry — resolves theme names to Theme instances."""

from __future__ import annotations

import json
from pathlib import Path

from agent.transports.themes.builtin import BUILTIN_THEMES
from agent.transports.themes.models import (
    FixedLayoutConfig,
    FooterStyle,
    HeaderStyle,
    LayoutConfig,
    ScrollbarConfig,
    Theme,
)

__all__ = [
    "FixedLayoutConfig",
    "FooterStyle",
    "HeaderStyle",
    "LayoutConfig",
    "ScrollbarConfig",
    "Theme",
    "ThemeRegistry",
]


class ThemeRegistry:
    """Resolves theme names to :class:`Theme` instances.

    Resolution order:
      1. Built-in themes (default, claude, bladerunner)
      2. User themes at ``~/.aar/themes/{name}.json``
      3. Absolute / relative path to a JSON file
    """

    def __init__(self) -> None:
        self._themes: dict[str, Theme] = dict(BUILTIN_THEMES)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, name: str) -> Theme:
        """Return a theme by *name*, loading from disk if necessary."""
        if name in self._themes:
            return self._themes[name]

        # Try ~/.aar/themes/{name}.json
        user_path = Path.home() / ".aar" / "themes" / f"{name}.json"
        if user_path.is_file():
            return self._load_json(user_path)

        # Try as a direct file path
        direct = Path(name)
        if direct.is_file():
            return self._load_json(direct)

        raise KeyError(f"Unknown theme: {name!r}")

    def register(self, theme: Theme) -> None:
        """Register a theme (overwrites any existing theme with the same name)."""
        self._themes[theme.name] = theme

    def list_names(self) -> list[str]:
        """Return sorted list of registered theme names."""
        # Include user themes on disk that haven't been loaded yet
        names = set(self._themes)
        user_dir = Path.home() / ".aar" / "themes"
        if user_dir.is_dir():
            for p in user_dir.glob("*.json"):
                names.add(p.stem)
        return sorted(names)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_json(self, path: Path) -> Theme:
        data = json.loads(path.read_text(encoding="utf-8"))
        theme = Theme.model_validate(data)
        self._themes[theme.name] = theme
        return theme
