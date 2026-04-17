"""Event, budget, and misc helpers for the core loop.

Extracted from :mod:`agent.core.loop` to keep the main runtime thin. Nothing
here understands the loop's control flow — each helper does one small thing
so it can be reused or tested in isolation.
"""

from __future__ import annotations

from agent.core.config import AgentConfig
from agent.core.events import ErrorEvent, StopReason
from agent.core.session import Session
from agent.core.state import AgentState
from agent.providers.base import ProviderResponse


def emit(session: Session, on_event, event) -> None:
    """Append an event to the session and fire the optional callback."""
    session.append(event)
    if on_event:
        on_event(event)


def emit_provider_observation(
    session: Session,
    on_event,
    response: ProviderResponse,
    provider_ms: float,
) -> None:
    """Emit metadata and reasoning blocks for a provider response."""
    if response.meta:
        response.meta.duration_ms = provider_ms
        emit(session, on_event, response.meta)

    for rb in response.reasoning:
        emit(session, on_event, rb)


def apply_usage_and_budget(
    session: Session,
    on_event,
    response: ProviderResponse,
    config: AgentConfig,
) -> bool:
    """Update usage totals and stop when a hard budget is exceeded.

    Returns *True* if the loop should exit (budget blown).
    """
    if not response.meta or not response.meta.usage:
        return False

    from agent.core.tokens import TokenUsage, calculate_cost, get_pricing

    usage = TokenUsage.from_dict(response.meta.usage)
    session.total_input_tokens += usage.input_tokens
    session.total_output_tokens += usage.output_tokens

    pricing = get_pricing(config.provider.model)
    if pricing:
        session.total_cost += calculate_cost(usage, pricing)

    if config.token_budget > 0 and session.total_tokens >= config.token_budget:
        session.state = AgentState.BUDGET_EXCEEDED
        emit(
            session,
            on_event,
            ErrorEvent(
                message=f"Token budget exceeded ({session.total_tokens}/{config.token_budget})",
                recoverable=False,
            ),
        )
        return True

    if config.cost_limit > 0 and session.total_cost >= config.cost_limit:
        session.state = AgentState.BUDGET_EXCEEDED
        emit(
            session,
            on_event,
            ErrorEvent(
                message=(
                    f"Cost limit exceeded (${session.total_cost:.4f}/${config.cost_limit:.4f})"
                ),
                recoverable=False,
            ),
        )
        return True

    return False


def append_internal_user_message(
    session: Session,
    on_event,
    content: str,
    *,
    reason: str,
) -> None:
    """Add a synthetic user message for loop-internal recovery flows."""
    message = session.add_user_message(content)
    message.data["internal"] = True
    message.data["reason"] = reason
    if on_event:
        on_event(message)


def parse_stop(reason: str) -> StopReason:
    """Parse a provider stop-reason string, falling back to ``END_TURN``."""
    try:
        return StopReason(reason)
    except ValueError:
        return StopReason.END_TURN
