"""CompanionPanel — the living ASCII companion for the fixed TUI.

A small animated creature called "Bit" lives in the upper-right panel.
It evolves across 5 levels (based on session step count), changes mood
in response to agent events, and reflects codebase health via git status.

Architecture
------------
- :class:`CompanionEngine` (imported from ``agent.transports.companion_state``)
  holds all mutable state — pure Python, no Textual dependencies.
- :class:`CompanionPanel` wraps it in a Textual ``Static`` widget.
  A ``set_interval(0.5, _tick)`` timer drives the animation.
- Public methods (``on_streaming``, ``on_thinking``, ``on_step``, ``on_error``,
  ``on_idle``, ``apply_git_health``) are called by :class:`FixedTUIRenderer`
  from within the Textual event loop — ``refresh()`` is safe there.

Bugs fixed vs. earlier draft
-----------------------------
- **Hang fix**: ``DEFAULT_CSS = ""`` was stripping ``Static``'s built-in
  ``height: auto`` rule.  Without it Textual cannot resolve the layout when
  this widget sits alongside a ``1fr`` sibling inside a ``Vertical``, causing
  an infinite layout-resolution loop.  The correct CSS is restored here.
- **Rich markup escape**: the XP bar was rendered as ``[######....]`` which
  Rich's markup parser would try (and silently fail) to interpret as a style
  tag.  Brackets are now escaped via ``\\[`` in the assembled ``Text`` output.
- **Timer**: ``set_interval`` is re-enabled now that the hang source is fixed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.core.session import Session

from rich.text import Text

try:
    from textual.widgets import Static
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "The fixed TUI requires the 'textual' package. "
        'Install it with: pip install "aar-agent[tui-fixed]"'
    ) from exc

from agent.transports.companion_state import CompanionEngine, GitHealth, Mood
from agent.transports.themes.models import CompanionConfig, Theme

# ---------------------------------------------------------------------------
# ASCII art frames
# ---------------------------------------------------------------------------
# Structure: _ART[level][mood.value] = list of frames
#            each frame = list of 4 strings (art lines)
# Lines are centred within the panel by the render method.
# ---------------------------------------------------------------------------

_ART: dict[int, dict[str, list[list[str]]]] = {
    1: {  # hatchling — basic blob
        "happy": [
            [" .----. ", "( o  o )", " )    ( ", "(_/  \\_)"],
            [" .----. ", "( o  o )", " ) ^^ ( ", "(_/  \\_)"],
        ],
        "sleeping": [
            [" .----. ", "( -  - )", " ) zzZ  ", "(_/  \\_)"],
            [" .----. ", "( -  - )", " ) Zzz  ", "(_/  \\_)"],
        ],
        "focused": [
            [" .----. ", "( -  . )", " )    ( ", "(_/  \\_)"],
        ],
        "thinking": [
            [" .----. ", "( ?  ? )", " ) .... ", "(_/  \\_)"],
            [" .----. ", "( ?  ? )", " )  ... ", "(_/  \\_)"],
        ],
        "excited": [
            [" .----. ", "( ^  ^ )", " ) !!  ", "(_/  \\_)"],
            [" .----. ", "( @  @ )", " )  !  ", "(_/  \\_)"],
        ],
        "stressed": [
            [" .----. ", "( >  < )", " ) ##  ", "(_/  \\_)"],
            [" .----. ", "( >  < )", " ) ~~  ", "(_/  \\_)"],
        ],
        "level_up": [
            [" .----. ", "( *  * )", " ) LV! ", "(_/  \\_)"],
            [" .----. ", "(  **  )", "  )UP! ", "(_/  \\_)"],
        ],
    },
    2: {  # gains pointed ears
        "happy": [
            ["  ^    ^  ", "( o  o  )", "  )    ( ", " (_/  \\_)"],
            ["  ^    ^  ", "( o  o  )", "  ) ^^ ( ", " (_/  \\_)"],
        ],
        "sleeping": [
            ["  ^    ^  ", "( -  -  )", "  ) zzZ  ", " (_/  \\_)"],
            ["  ^    ^  ", "( -  -  )", "  ) Zzz  ", " (_/  \\_)"],
        ],
        "focused": [
            ["  ^    ^  ", "( -  .  )", "  )    ( ", " (_/  \\_)"],
        ],
        "thinking": [
            ["  ^    ^  ", "( ?  ?  )", "  ) .... ", " (_/  \\_)"],
            ["  ^    ^  ", "( ?  ?  )", "  )  ... ", " (_/  \\_)"],
        ],
        "excited": [
            [" ^^    ^^ ", "( ^  ^  )", "  ) !!  ", " (_/  \\_)"],
            [" ^^    ^^ ", "( @  @  )", "  )  !  ", " (_/  \\_)"],
        ],
        "stressed": [
            ["  v    v  ", "( >  <  )", "  ) ##  ", " (_/  \\_)"],
            ["  v    v  ", "( >  <  )", "  ) ~~  ", " (_/  \\_)"],
        ],
        "level_up": [
            ["  *    *  ", "( *  *  )", "  ) LV! ", " (_/  \\_)"],
            [" ******* ", "( ** **  )", "   )UP! ", " (_/  \\_)"],
        ],
    },
    3: {  # wing hints appear
        "happy": [
            ["~.-----. ~", "( o   o )", "  )    ( ", " (_/  \\_)"],
            ["~.-----. ~", "( o   o )", "  ) ^^ ( ", " (_/  \\_)"],
        ],
        "sleeping": [
            ["~.-----. ~", "( -   - )", "  ) zzZ  ", " (_/  \\_)"],
            ["~.-----. ~", "( -   - )", "  ) Zzz  ", " (_/  \\_)"],
        ],
        "focused": [
            ["-.-----. -", "( -   . )", "  )    ( ", " (_/  \\_)"],
        ],
        "thinking": [
            ["~.-----. ~", "( ?   ? )", "  ) .... ", " (_/  \\_)"],
            ["~.-----. ~", "( ?   ? )", "  )  ... ", " (_/  \\_)"],
        ],
        "excited": [
            ["^.-----. ^", "( ^   ^ )", "  ) !!  ", " (_/  \\_)"],
            ["^.-----. ^", "( @   @ )", "  )  !  ", " (_/  \\_)"],
        ],
        "stressed": [
            ["-.-----. -", "( >   < )", "  ) ##  ", " (_/  \\_)"],
            ["-.-----. -", "( >   < )", "  ) ~~  ", " (_/  \\_)"],
        ],
        "level_up": [
            ["*.-----. *", "( *   * )", "  ) LV! ", " (_/  \\_)"],
            ["*.*****.*", "(  *** )", "   )UP! ", " (_/  \\_)"],
        ],
    },
    4: {  # glowing eyes
        "happy": [
            ["*.------.*", "(Oo    oO)", " ) **  ( ", "(________)"],
            ["*.------.*", "(Oo    oO)", " ) ^^  ( ", "(________)"],
        ],
        "sleeping": [
            ["*.------.*", "(O-    -O)", " ) zzZ   ", "(________)"],
            ["*.------.*", "(O-    -O)", " ) Zzz   ", "(________)"],
        ],
        "focused": [
            ["*.------.*", "(O-    .O)", " )     ( ", "(________)"],
        ],
        "thinking": [
            ["*.------.*", "(O?    ?O)", " ) ....  ", "(________)"],
            ["*.------.*", "(O?    ?O)", " )  ...  ", "(________)"],
        ],
        "excited": [
            ["*.------.*", "(O^    ^O)", " ) !!   ", "(________)"],
            ["*.------.*", "(O@    @O)", " )  !   ", "(________)"],
        ],
        "stressed": [
            ["*.------.*", "(O>    <O)", " ) ##   ", "(________)"],
            ["*.------.*", "(O>    <O)", " ) ~~   ", "(________)"],
        ],
        "level_up": [
            ["*.------.*", "(O*    *O)", " ) LV!  ", "(________)"],
            ["*.*****.**", "(  ****  )", "  )UP!  ", "(________)"],
        ],
    },
    5: {  # cosmic being
        "happy": [
            ["+*-----*+", "(+o   o+)", " )+   +( ", "(+_____+)"],
            ["+*-----*+", "(+o   o+)", " )+ * +( ", "(+_____+)"],
        ],
        "sleeping": [
            ["+*-----*+", "(+-   -+)", " )+zzZ+  ", "(+_____+)"],
            ["+*-----*+", "(+-   -+)", " )+Zzz+  ", "(+_____+)"],
        ],
        "focused": [
            ["+*-----*+", "(+-   .+)", " )+   +( ", "(+_____+)"],
        ],
        "thinking": [
            ["+*-----*+", "(+?   ?+)", " )+...+  ", "(+_____+)"],
            ["+*-----*+", "(+?   ?+)", " )+ ..+  ", "(+_____+)"],
        ],
        "excited": [
            ["+*-----*+", "(+^   ^+)", " )+!! +( ", "(+_____+)"],
            ["+*-----*+", "(+@   @+)", " )+ ! +( ", "(+_____+)"],
        ],
        "stressed": [
            ["+*-----*+", "(+>   <+)", " )+## +( ", "(+_____+)"],
            ["+*-----*+", "(+>   <+)", " )+~~ +( ", "(+_____+)"],
        ],
        "level_up": [
            ["+*-----*+", "(+*   *+)", " )+LV!+( ", "(+_____+)"],
            ["+*****+", "(+ *** +)", "  )+UP!  ", "(+_____+)"],
        ],
    },
}

# ---------------------------------------------------------------------------
# Display metadata
# ---------------------------------------------------------------------------

_MOOD_LABELS: dict[str, str] = {
    "happy": "happy  :)",
    "sleeping": "zzz...",
    "focused": "focused",
    "thinking": "thinking...",
    "excited": "excited!",
    "stressed": "stressed!",
    "level_up": "LEVEL UP!",
}

_MOOD_STYLES: dict[str, str] = {
    "happy": "bold green",
    "sleeping": "dim blue",
    "focused": "cyan",
    "thinking": "magenta",
    "excited": "bold yellow",
    "stressed": "bold red",
    "level_up": "bold gold1",
}

_LEVEL_ART_STYLES: dict[int, str] = {
    1: "dim white",
    2: "white",
    3: "cyan",
    4: "green",
    5: "gold1",
}

_XP_BAR_WIDTH = 10
_PANEL_RENDER_WIDTH = 22  # art lines are centred within this width


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------


class CompanionPanel(Static):
    """Animated ASCII companion widget — lives in the upper portion of the right column.

    Public event-hook methods (``on_streaming``, ``on_thinking``, ``on_step``,
    ``on_error``, ``on_idle``, ``apply_git_health``) are called by
    :class:`~agent.transports.tui_fixed.FixedTUIRenderer` from within the
    Textual event loop.

    CSS notes
    ---------
    We deliberately inherit ``Static``'s ``height: auto`` default so that
    Textual can measure the rendered content and give the widget the correct
    height.  Without ``height: auto`` the layout solver cannot resolve heights
    when this widget shares a ``Vertical`` container with a ``1fr`` sibling
    (``ThinkingPanel``), causing an infinite layout loop / hang.
    """

    # -----------------------------------------------------------------------
    # IMPORTANT: do NOT set DEFAULT_CSS = "" here.
    #
    # Static ships with:
    #   Static { height: auto; }
    #
    # That rule tells Textual to measure the rendered content and use its
    # natural height.  Overriding with an empty string removes the rule,
    # leaving the widget with an undefined / zero height.  Inside a Vertical
    # container that also has a `1fr` sibling the layout solver then enters
    # an infinite resolution loop — the app hangs without ever mounting.
    #
    # If you need to add extra rules, *extend* the parent's CSS:
    #   DEFAULT_CSS = Static.DEFAULT_CSS + "\nCompanionPanel { width: 1fr; }"
    # -----------------------------------------------------------------------

    def __init__(self, theme: Theme, config: CompanionConfig) -> None:
        super().__init__()
        self._theme = theme
        self._config = config
        self._engine = CompanionEngine()
        self._frame_idx: int = 0

    # ------------------------------------------------------------------
    # Textual lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        """Start the 0.5 s animation timer."""
        self.set_interval(0.5, self._tick)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self) -> Text:
        """Build the companion display as a ``rich.text.Text`` object.

        Returning ``Text`` (rather than a plain ``str``) means:
        - Per-segment styling is applied without touching Rich markup syntax.
        - No risk of ``[…]`` sequences in the XP bar being misread as markup.
        """
        engine = self._engine
        mood = engine.mood
        level = engine.level
        cfg = self._config

        art_style = _LEVEL_ART_STYLES.get(level, "white")
        mood_style = _MOOD_STYLES.get(mood.value, "white")

        level_frames = _ART.get(level, _ART[1])
        mood_frames = level_frames.get(mood.value, level_frames.get("happy", _ART[1]["happy"]))
        frame = mood_frames[self._frame_idx % len(mood_frames)]

        w = _PANEL_RENDER_WIDTH
        name = cfg.name
        title_line = f"-- {name}  lv.{level} --"

        filled = int(engine.xp * _XP_BAR_WIDTH)
        # Build XP bar characters — deliberately avoid Rich markup brackets by
        # constructing the Text segments without passing them through markup.
        bar_filled = "#" * filled
        bar_empty = "." * (_XP_BAR_WIDTH - filled)

        mood_label = _MOOD_LABELS.get(mood.value, mood.value)
        sep_line = "-" * w

        t = Text(no_wrap=False, overflow="fold")

        # Title row
        t.append(title_line.center(w) + "\n", style="dim")

        # Art rows
        for line in frame:
            t.append(line.center(w) + "\n", style=art_style)

        # Mood label
        t.append(mood_label.center(w) + "\n", style=mood_style)

        # XP bar — build without any bracket-containing f-strings going
        # through markup; each piece is appended as a plain Text segment.
        bar_line_left = " " * max((w - _XP_BAR_WIDTH - 4) // 2, 0)
        t.append(bar_line_left + "[", style="dim")
        t.append(bar_filled, style=mood_style)
        t.append(bar_empty, style="dim")
        t.append(f"] {engine.steps}s\n", style="dim")

        # Separator
        t.append(sep_line + "\n", style="dim")

        return t

    # ------------------------------------------------------------------
    # Animation tick
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """Advance animation frame and update engine state."""
        self._frame_idx += 1
        self._engine.tick()
        self.refresh()

    # ------------------------------------------------------------------
    # Event hooks — called by FixedTUIRenderer
    # ------------------------------------------------------------------

    def agent_streaming(self) -> None:
        """LLM started emitting answer tokens."""
        self._engine.on_streaming()
        self.refresh()

    def agent_thinking(self) -> None:
        """LLM started emitting reasoning tokens."""
        self._engine.on_thinking()
        self.refresh()

    def agent_step(self) -> None:
        """A tool call was made."""
        self._engine.on_step()
        self.refresh()

    def agent_error(self) -> None:
        """An error occurred."""
        self._engine.on_error()
        self.refresh()

    def agent_idle(self) -> None:
        """Agent run completed."""
        self._engine.on_idle()
        self.refresh()

    def apply_git_health(self, health: GitHealth) -> None:
        """Update git health and let the engine adjust the resting mood."""
        self._engine.apply_git_health(health)
        self.refresh()

    def apply_theme(self, theme: Theme) -> None:
        """Update theme reference (called on theme cycle)."""
        self._theme = theme
        self.refresh()

    def bootstrap_from_session(self, session: "Session") -> None:
        """Restore companion progress from a resumed session.

        Called by :class:`~agent.transports.tui_fixed.AarFixedApp` immediately
        after loading a ``--session``.  Progress is derived purely from the
        session's event history — no separate companion save-file is needed.

        The companion level and step count are recovered by scanning the
        session's ``ToolCall`` events plus any ``companion_baseline`` watermark
        stored in ``session.metadata`` (written by ``SessionStore.compact()``
        before it prunes old events).  This means progress is infinite and
        survives sliding-window context compaction transparently.
        """
        self._engine.bootstrap_from_session(session)
        self.refresh()
