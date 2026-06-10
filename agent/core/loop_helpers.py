"""Event, budget, and misc helpers for the core loop.

Extracted from :mod:`agent.core.loop` to keep the main runtime thin. Nothing
here understands the loop's control flow — each helper does one small thing
so it can be reused or tested in isolation.
"""

from __future__ import annotations

import json

from agent.core.config import AgentConfig
from agent.core.events import ErrorEvent, StopReason, ToolCall
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

    pricing = get_pricing(config.resolve_provider().model)
    if pricing:
        session.total_cost += calculate_cost(usage, pricing)

    _token_budget = config.effective_token_budget()
    if _token_budget > 0 and session.total_tokens >= _token_budget:
        session.state = AgentState.BUDGET_EXCEEDED
        emit(
            session,
            on_event,
            ErrorEvent(
                message=f"Token budget exceeded ({session.total_tokens}/{_token_budget})",
                recoverable=False,
            ),
        )
        return True

    _cost_limit = config.effective_cost_limit()
    if _cost_limit > 0 and session.total_cost >= _cost_limit:
        session.state = AgentState.BUDGET_EXCEEDED
        emit(
            session,
            on_event,
            ErrorEvent(
                message=(f"Cost limit exceeded (${session.total_cost:.4f}/${_cost_limit:.4f})"),
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


def detect_truncated_tool_call(
    response: ProviderResponse,
    max_tokens: int,
) -> tuple[ToolCall, str] | None:
    """Detect a ``max_tokens``-induced tool-argument truncation.

    Some providers (notably Anthropic) return ``stop_reason="tool_use"`` —
    *not* ``"max_tokens"`` — when a ``tool_use`` block's argument JSON is cut
    off because the response hit the configured ``max_tokens`` cap. The
    arguments come back as a partial, unparsable JSON string, which the tool
    dispatcher then rejects with ``invalid_arguments``. The model retries the
    same call, hits the same cap, and the loop spins.

    Returns ``(tool_call, raw_payload)`` when **all** of the following hold:

    1. The response contains at least one tool-use block.
    2. ``usage.output_tokens >= max_tokens`` (i.e. the cap was reached).
    3. The tool-call arguments cannot be parsed as JSON.

    Returns ``None`` otherwise. The check is deliberately defensive about
    missing ``meta``/``usage`` fields so it works with any provider, including
    streaming collectors that surface the unparsable payload as
    ``arguments={"raw": <partial JSON>}``.
    """
    if max_tokens <= 0:
        return None
    if not response.tool_calls:
        return None
    if not response.meta or not response.meta.usage:
        return None

    output_tokens = response.meta.usage.get("output_tokens", 0) or 0
    if output_tokens < max_tokens:
        return None

    for tc in response.tool_calls:
        args = tc.arguments
        if not isinstance(args, dict):
            continue

        # Streaming-collector fallback: both Anthropic and OpenAI adapters
        # store the raw partial JSON under a single ``"raw"`` key when the
        # accumulated argument string fails ``json.loads``. A well-formed
        # tool call never produces this shape, so seeing it is a strong
        # signal that the JSON was truncated mid-stream.
        if set(args.keys()) == {"raw"}:
            raw = args.get("raw")
            if isinstance(raw, str):
                try:
                    json.loads(raw)
                except (ValueError, TypeError):
                    return tc, raw
                # Parseable raw payload — the model legitimately maxed out
                # while emitting valid JSON; not a truncation event.
                continue

        # Some providers may stash the unparsed payload on event metadata.
        truncated_raw = tc.data.get("truncated_arguments") if tc.data else None
        if isinstance(truncated_raw, str):
            try:
                json.loads(truncated_raw)
            except (ValueError, TypeError):
                return tc, truncated_raw

    return None
