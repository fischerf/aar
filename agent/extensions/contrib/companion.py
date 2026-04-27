"""Companion extension — wraps :class:`CompanionEngine` as an Aar extension.

This is a proof-of-concept showing how the living-companion state engine
(mood, level, XP) can be surfaced through the upcoming extension API.  The
extension hooks into agent lifecycle events to drive mood transitions and
exposes a ``companion_status`` tool so the LLM (or the user) can query the
companion's current state.

Usage (once the extension API is wired up)::

    # In config.json → extensions:
    { "module": "agent.extensions.contrib.companion" }
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent.extensions.api import ExtensionAPI, ExtensionContext
from agent.transports.companion_state import CompanionEngine, Mood, xp_fraction

if TYPE_CHECKING:
    from agent.transports.companion_state import GitHealth

# ---------------------------------------------------------------------------
# Extension entry-point
# ---------------------------------------------------------------------------

# The ``register`` function is the contract between an extension module and
# the Aar extension loader.  It receives an ``ExtensionAPI`` instance whose
# decorators wire callbacks into the agent event stream.


def register(api: ExtensionAPI) -> None:
    """Register companion event hooks and the ``companion_status`` tool."""

    engine: CompanionEngine | None = None

    # -- lifecycle events ---------------------------------------------------

    @api.on("session_start")
    def _on_session_start(event: Any, ctx: ExtensionContext) -> None:
        nonlocal engine
        engine = CompanionEngine()
        if ctx.session is not None:
            engine.bootstrap_from_session(ctx.session)
        ctx.logger.debug(
            "companion: initialised — level=%d steps=%d mood=%s",
            engine.level,
            engine.steps,
            engine.mood.value,
        )

    @api.on("tool_call")
    def _on_tool_call(event: Any, ctx: ExtensionContext) -> None:
        if engine is None:
            return
        levelled_up = engine.on_step()
        if levelled_up:
            ctx.logger.info(
                "companion: level up! now level %d (%d steps)",
                engine.level,
                engine.steps,
            )

    @api.on("stream_chunk")
    def _on_stream_chunk(event: Any, ctx: ExtensionContext) -> None:
        if engine is None:
            return
        if getattr(event, "reasoning_text", None):
            engine.on_thinking()
        else:
            engine.on_streaming()

    @api.on("error")
    def _on_error(event: Any, ctx: ExtensionContext) -> None:
        if engine is None:
            return
        engine.on_error()
        ctx.logger.debug("companion: error recorded — total errors=%d", engine.errors)

    @api.on("session_end")
    def _on_session_end(event: Any, ctx: ExtensionContext) -> None:
        if engine is None:
            return
        engine.on_idle()
        ctx.logger.debug("companion: session ended — mood=%s", engine.mood.value)

    # -- tool ---------------------------------------------------------------

    @api.tool(
        name="companion_status",
        description=(
            "Return the living companion's current mood, level, XP progress, "
            "step count, and error count as a human-readable string."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    def _companion_status(ctx: ExtensionContext) -> str:
        if engine is None:
            return "companion not initialised (no active session)"

        xp = xp_fraction(engine.steps, engine.level)
        xp_pct = f"{xp * 100:.0f}%"

        lines = [
            f"mood:   {engine.mood.value}",
            f"level:  {engine.level} / 5",
            f"xp:     {xp_pct}",
            f"steps:  {engine.steps}",
            f"errors: {engine.errors}",
        ]

        if engine.mood is Mood.SLEEPING:
            lines.append("(zzz… the companion is napping)")
        elif engine.mood is Mood.LEVEL_UP:
            lines.append("✨ levelling up!")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Convenience helpers (usable outside the extension API)
# ---------------------------------------------------------------------------


def apply_git_health(engine: CompanionEngine, health: GitHealth) -> None:
    """Forward a git-health snapshot to the engine.

    Useful when a host (e.g. the TUI) runs its own periodic git probe and
    wants to keep the extension-managed engine in sync.
    """
    engine.apply_git_health(health)
