"""Lean runtime guardrails — mechanical safety nets for the agent loop."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from agent.core.events import ToolCall
from agent.core.session import Session

_STATE_KEY = "guardrails"

_MAX_TOKENS_FOLLOWUP = (
    "Your previous response was truncated by the token limit. "
    "Continue from exactly where you left off. Do not restart or repeat content."
)


class GuardrailsConfig(BaseModel):
    """Tuning knobs for the mechanical guardrails."""

    max_tokens_recoveries: int = 2
    max_repeated_tool_steps: int = 3
    reserve_tokens: int = 512
    reserve_cost_fraction: float = 0.1


def _get_state(session: Session) -> dict[str, Any]:
    """Return (and lazily initialise) the guardrails sub-dict in session metadata."""
    if _STATE_KEY not in session.metadata:
        session.metadata[_STATE_KEY] = {
            "max_tokens_recovery_count": 0,
            "last_tool_signature": None,
            "repeated_tool_steps": 0,
        }
    return session.metadata[_STATE_KEY]


class LoopGuardrails:
    """Stateless helper that reads/writes guardrail counters on a :class:`Session`.

    All mutable state lives in ``session.metadata["guardrails"]`` so that
    it is automatically persisted and restored with the session.
    """

    def __init__(self, config: GuardrailsConfig | None = None) -> None:
        self.config = config or GuardrailsConfig()

    # ------------------------------------------------------------------
    # Max-tokens recovery
    # ------------------------------------------------------------------

    def should_continue_after_max_tokens(self, session: Session) -> bool:
        """Return *True* if the loop may retry after a ``max_tokens`` stop.

        Each call increments the recovery counter.  Once the configured
        limit (default 2) is reached, returns *False*.
        """
        state = _get_state(session)
        if state["max_tokens_recovery_count"] >= self.config.max_tokens_recoveries:
            return False
        state["max_tokens_recovery_count"] += 1
        return True

    def max_tokens_followup(self) -> str:
        """Return the continuation prompt injected after a truncation."""
        return _MAX_TOKENS_FOLLOWUP

    # ------------------------------------------------------------------
    # Repetition detection
    # ------------------------------------------------------------------

    def observe_tool_calls(self, session: Session, tool_calls: list[ToolCall]) -> None:
        """Record the current tool-call signature and update the repetition counter."""
        state = _get_state(session)
        signature = _tool_signature(tool_calls)
        if signature == state["last_tool_signature"]:
            state["repeated_tool_steps"] += 1
        else:
            state["repeated_tool_steps"] = 0
            state["last_tool_signature"] = signature

    def is_stuck(self, session: Session) -> bool:
        """Return *True* when the same tool-call pattern has repeated too many times."""
        state = _get_state(session)
        return state["repeated_tool_steps"] >= self.config.max_repeated_tool_steps

    # ------------------------------------------------------------------
    # Budget proximity
    # ------------------------------------------------------------------

    def near_budget(
        self,
        session: Session,
        token_budget: int,
        cost_limit: float,
    ) -> bool:
        """Return *True* when remaining tokens or cost is within the reserve margin.

        Returns *False* immediately when the corresponding limit is zero
        (unlimited), so callers don't need to gate on that themselves.
        """
        if token_budget > 0:
            remaining_tokens = token_budget - session.total_tokens
            if remaining_tokens <= self.config.reserve_tokens:
                return True
        if cost_limit > 0:
            remaining_cost = cost_limit - session.total_cost
            if remaining_cost <= cost_limit * self.config.reserve_cost_fraction:
                return True
        return False


def _tool_signature(tool_calls: list[ToolCall]) -> str:
    """Derive a deterministic string from tool names and argument key-value pairs.

    Includes argument *values* so that calling the same tool with different
    arguments (e.g. reading different files) is not treated as a repetition.
    Values are truncated to keep the signature compact.
    """
    parts: list[str] = []
    for tc in tool_calls:
        items = sorted(tc.arguments.items())
        args = ",".join(f"{k}={_compact(v)}" for k, v in items)
        parts.append(f"{tc.tool_name}({args})")
    return ";".join(sorted(parts))


def _compact(value: object) -> str:
    """Truncate a value for signature comparison."""
    s = str(value)
    return s[:200] if len(s) > 200 else s
