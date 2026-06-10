"""Living companion state engine — pure logic, no Textual/UI imports.

The ``CompanionEngine`` is a lightweight state machine that tracks mood
and level for the ASCII companion widget.  All methods are synchronous and
side-effect-free (no I/O, no Textual) so the engine is easy to unit-test.

Mood transitions
----------------
Triggered by the agent event feed:

- tool_call      → on_step()      → FOCUSED (or LEVEL_UP on level threshold)
- stream text    → on_streaming() → EXCITED
- stream reason  → on_thinking()  → THINKING
- error          → on_error()     → STRESSED
- agent idle     → on_idle()      → HAPPY / FOCUSED / STRESSED (based on git)
- animation tick → tick()         → SLEEPING after ~30 s of silence

Level thresholds (steps = tool calls)
--------------------------------------
Level 1:  0 steps  — hatchling
Level 2:  5 steps  — ears appear
Level 3: 15 steps  — wings begin
Level 4: 30 steps  — glowing
Level 5: 50 steps  — cosmic
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.core.session import Session

from dataclasses import dataclass
from enum import Enum

# ---------------------------------------------------------------------------
# Mood
# ---------------------------------------------------------------------------


class Mood(str, Enum):
    HAPPY = "happy"
    SLEEPING = "sleeping"
    FOCUSED = "focused"
    THINKING = "thinking"
    EXCITED = "excited"
    STRESSED = "stressed"
    LEVEL_UP = "level_up"


# ---------------------------------------------------------------------------
# Git health
# ---------------------------------------------------------------------------


@dataclass
class GitHealth:
    """Snapshot of the working-tree state returned by ``git status --porcelain``."""

    dirty_files: int = 0
    untracked_files: int = 0

    @property
    def total_issues(self) -> int:
        return self.dirty_files + self.untracked_files

    @property
    def stress_level(self) -> int:
        """0 = clean, 1 = minor chaos (1-4 issues), 2 = full chaos (5+)."""
        t = self.total_issues
        if t == 0:
            return 0
        return 1 if t < 5 else 2


# ---------------------------------------------------------------------------
# Level system
# ---------------------------------------------------------------------------

#: Minimum accumulated steps (tool calls) to *enter* each level (1-indexed).
LEVEL_THRESHOLDS: tuple[int, ...] = (0, 5, 15, 30, 50)


def steps_to_level(steps: int) -> int:
    """Return companion level (1–5) for *steps* tool calls."""
    level = 1
    for threshold in LEVEL_THRESHOLDS[1:]:
        if steps >= threshold:
            level += 1
        else:
            break
    return min(level, 5)


def xp_fraction(steps: int, level: int) -> float:
    """Progress within the current level as a fraction 0.0–1.0."""
    if level >= 5:
        return 1.0
    cur = LEVEL_THRESHOLDS[level - 1]
    nxt = LEVEL_THRESHOLDS[level]
    return min((steps - cur) / max(nxt - cur, 1), 1.0)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class CompanionEngine:
    """Manages companion mood and level based on agent events.

    Intended for use by :class:`~agent.transports.tui_widgets.companion.CompanionPanel`;
    all methods are safe to call from synchronous code inside the Textual event loop.
    """

    #: Animation ticks before the companion falls asleep (~30 s at 0.5 s/tick).
    SLEEP_TICKS: int = 60
    #: How many ticks to display the level-up animation (~4 s at 0.5 s/tick).
    LEVELUP_TICKS: int = 8

    def __init__(self) -> None:
        self.mood: Mood = Mood.HAPPY
        self.level: int = 1
        self.steps: int = 0
        self.errors: int = 0
        self.git_health: GitHealth = GitHealth()
        self._idle_ticks: int = 0
        self._level_up_ticks: int = 0

    # ------------------------------------------------------------------
    # Event hooks
    # ------------------------------------------------------------------

    def on_step(self) -> bool:
        """A tool call occurred.  Returns ``True`` if the companion just levelled up."""
        self.steps += 1
        self._idle_ticks = 0
        old_level = self.level
        self.level = steps_to_level(self.steps)
        if self.level > old_level:
            self.mood = Mood.LEVEL_UP
            self._level_up_ticks = self.LEVELUP_TICKS
            return True
        if self.mood != Mood.LEVEL_UP:
            self.mood = Mood.FOCUSED
        return False

    def on_streaming(self) -> None:
        """LLM started emitting answer tokens."""
        self._idle_ticks = 0
        if self.mood != Mood.LEVEL_UP:
            self.mood = Mood.EXCITED

    def on_thinking(self) -> None:
        """LLM started emitting reasoning tokens."""
        self._idle_ticks = 0
        if self.mood != Mood.LEVEL_UP:
            self.mood = Mood.THINKING

    def on_error(self) -> None:
        """An error event was received."""
        self.errors += 1
        if self.mood != Mood.LEVEL_UP:
            self.mood = Mood.STRESSED

    def on_idle(self) -> None:
        """Agent run completed; settle into a resting mood."""
        self._idle_ticks = 0
        if self.mood == Mood.LEVEL_UP:
            return  # level-up animation still running
        if self.git_health.stress_level == 2:
            self.mood = Mood.STRESSED
        elif self.git_health.stress_level == 1:
            self.mood = Mood.FOCUSED
        else:
            self.mood = Mood.HAPPY

    def apply_git_health(self, health: GitHealth) -> None:
        """Update git health and refresh resting mood if not actively busy."""
        self.git_health = health
        if self.mood in (Mood.HAPPY, Mood.FOCUSED, Mood.STRESSED, Mood.SLEEPING):
            self.on_idle()

    def bootstrap_from_session(self, session: "Session") -> None:
        """Seed engine state from a loaded session without replaying events.

        Called when the TUI resumes a ``--session`` so the companion's
        accumulated progress (level, steps, errors) is restored.  Mood is
        initialised to the calm resting state; the periodic git-health poll
        will update it within the first poll interval.

        The session needs no special companion-specific fields: progress is
        derived from the ``ToolCall`` and ``ErrorEvent`` counts in
        ``session.events``, augmented by the ``companion_baseline`` watermark
        that ``SessionStore.compact()`` writes before truncating old events.
        """
        stats = companion_stats_from_session(session)
        self.steps = stats["steps"]
        self.errors = stats["errors"]
        self.level = steps_to_level(self.steps)
        # Settle into a resting mood; git health poll will refine it shortly.
        self.mood = Mood.HAPPY
        self._level_up_ticks = 0
        self._idle_ticks = 0
        # on_idle() respects the git_health already stored on the engine
        self.on_idle()

    def tick(self) -> None:
        """Animation tick (~2x per second).

        - Decrements level-up countdown and falls back to idle mood when done.
        - Triggers sleep after prolonged inactivity.
        """
        if self.mood == Mood.LEVEL_UP:
            self._level_up_ticks -= 1
            if self._level_up_ticks <= 0:
                # Clear LEVEL_UP first so on_idle()'s guard doesn't short-circuit.
                self.mood = Mood.HAPPY
                self.on_idle()
            return
        self._idle_ticks += 1
        if self._idle_ticks > self.SLEEP_TICKS:
            self.mood = Mood.SLEEPING

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    @property
    def xp(self) -> float:
        """XP progress within the current level (0.0–1.0)."""
        return xp_fraction(self.steps, self.level)


# ---------------------------------------------------------------------------
# Session-derived progress
# ---------------------------------------------------------------------------


def companion_stats_from_session(session: "Session") -> dict[str, int]:
    """Derive companion stats from a session's event history + compaction baseline.

    Returns ``{"steps": N, "errors": N}`` where:

    - ``steps``  — total ``ToolCall`` events across the session's entire
      lifetime, including any that were pruned by ``SessionStore.compact()``.
      Compaction preserves a ``companion_baseline`` watermark in
      ``session.metadata`` so progress is never lost.
    - ``errors`` — same, for ``ErrorEvent`` occurrences.

    This is a pure function of the session object — no I/O, no side-effects.
    """
    from agent.core.events import ErrorEvent, ToolCall  # lazy: avoids top-level cross-layer import

    baseline: dict[str, int] = session.metadata.get("companion_baseline", {})
    base_steps = int(baseline.get("steps", 0))
    base_errors = int(baseline.get("errors", 0))

    steps = base_steps + sum(1 for e in session.events if isinstance(e, ToolCall))
    errors = base_errors + sum(1 for e in session.events if isinstance(e, ErrorEvent))
    return {"steps": steps, "errors": errors}


def companion_on_prune(pruned: list, metadata: dict) -> None:
    """on_prune hook for SessionStore.compact() — maintains companion_baseline.

    Rolls up ``ToolCall`` and ``ErrorEvent`` counts from the about-to-be-pruned
    events into ``metadata["companion_baseline"]`` so that
    :func:`companion_stats_from_session` can recover lifetime totals after
    compaction.

    Usage::

        from agent.transports.companion_state import companion_on_prune
        store.compact(session_id, max_events=200, on_prune=companion_on_prune)
    """
    from agent.core.events import ErrorEvent, ToolCall

    prior = metadata.get("companion_baseline", {})
    base_steps = int(prior.get("steps", 0))
    base_errors = int(prior.get("errors", 0))
    metadata["companion_baseline"] = {
        "steps": base_steps + sum(1 for e in pruned if isinstance(e, ToolCall)),
        "errors": base_errors + sum(1 for e in pruned if isinstance(e, ErrorEvent)),
    }


# ---------------------------------------------------------------------------
# Git probe
# ---------------------------------------------------------------------------


async def get_git_health(cwd: str | None = None) -> GitHealth:
    """Run ``git status --porcelain`` and return a :class:`GitHealth` snapshot.

    Never raises — returns an empty :class:`GitHealth` on any failure (no git
    repo, git not installed, timeout, etc.).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--porcelain",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=cwd,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        lines = stdout.decode("utf-8", errors="replace").splitlines()
        dirty = sum(1 for ln in lines if ln and not ln.startswith("??"))
        untracked = sum(1 for ln in lines if ln.startswith("??"))
        return GitHealth(dirty_files=dirty, untracked_files=untracked)
    except Exception:
        return GitHealth()
