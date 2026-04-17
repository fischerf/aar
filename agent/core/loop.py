"""Thin core loop — the heart of the agent runtime.

Everything that isn't loop control flow lives elsewhere:

- :mod:`agent.core.provider_runner` — retries, streaming, error translation
- :mod:`agent.core.loop_helpers`   — event emission, usage/budget, misc utilities
- :mod:`agent.core.guardrails`     — repetition detection, budget proximity, max-token recovery
"""

from __future__ import annotations

import asyncio
import logging
import time

from agent.core.config import AgentConfig
from agent.core.events import AssistantMessage, ErrorEvent, StopReason
from agent.core.guardrails import LoopGuardrails
from agent.core.loop_helpers import (
    append_internal_user_message,
    apply_usage_and_budget,
    emit,
    emit_provider_observation,
    parse_stop,
)
from agent.core.provider_runner import ProviderRequestFailed, provider_request
from agent.core.session import Session, trim_to_token_budget
from agent.core.state import AgentState
from agent.providers.base import Provider
from agent.tools.execution import ToolExecutor

logger = logging.getLogger(__name__)


async def run_loop(
    session: Session,
    provider: Provider,
    tool_executor: ToolExecutor,
    config: AgentConfig,
    on_event=None,
    cancel_event: asyncio.Event | None = None,
) -> Session:
    """Run the agent loop until completion, max steps, or timeout.

    Args:
        session: The current session with conversation history.
        provider: The LLM provider to use.
        tool_executor: Executor for tool calls.
        config: Agent configuration.
        on_event: Optional callback called with each new event.
        cancel_event: Optional asyncio.Event; set it to request cooperative cancellation.

    Returns:
        The updated session.
    """
    session.state = AgentState.RUNNING
    start_time = time.monotonic()
    done = False
    guardrails = LoopGuardrails(config.guardrails)

    log = logger.getChild("loop")
    log_extra = {"session_id": session.session_id, "trace_id": session.trace_id}

    try:
        while not done and session.step_count < config.max_steps:
            if cancel_event is not None and cancel_event.is_set():
                session.state = AgentState.CANCELLED
                emit(session, on_event, ErrorEvent(message="Agent cancelled", recoverable=False))
                return session

            if config.timeout > 0.0 and time.monotonic() - start_time > config.timeout:
                session.state = AgentState.TIMED_OUT
                emit(
                    session,
                    on_event,
                    ErrorEvent(
                        message=f"Agent timed out after {config.timeout}s", recoverable=False
                    ),
                )
                return session

            session.increment_step()
            messages = session.to_messages()
            if config.context_window > 0 and config.context_strategy == "sliding_window":
                messages = trim_to_token_budget(messages, config.context_window)

            tool_schemas = tool_executor.registry.to_provider_schemas() or None
            try:
                response, provider_ms = await provider_request(
                    provider=provider,
                    messages=messages,
                    tool_schemas=tool_schemas,
                    system_prompt=config.system_prompt,
                    session=session,
                    on_event=on_event,
                    config=config,
                    use_streaming=config.streaming and provider.supports_streaming,
                    log=log,
                    log_extra=log_extra,
                )
            except ProviderRequestFailed:
                return session

            emit_provider_observation(session, on_event, response, provider_ms)
            if apply_usage_and_budget(session, on_event, response, config):
                return session

            if guardrails.check_near_budget(session, config.token_budget, config.cost_limit):
                log.warning(
                    "Near budget at step %d (tokens=%d budget=%d cost=%.4f limit=%.4f)",
                    session.step_count,
                    session.total_tokens,
                    config.token_budget,
                    session.total_cost,
                    config.cost_limit,
                    extra=log_extra,
                )
                emit(
                    session,
                    on_event,
                    ErrorEvent(
                        message="Approaching budget limit — stopping soon", recoverable=True
                    ),
                )

            log.info(
                "step=%d provider_ms=%.0f tool_calls=%d",
                session.step_count,
                provider_ms,
                len(response.tool_calls),
                extra=log_extra,
            )

            if response.tool_calls:
                guardrails.observe_tool_calls(session, response.tool_calls)
                if guardrails.is_stuck(session):
                    log.warning(
                        "Repetition guard triggered at step %d", session.step_count, extra=log_extra
                    )
                    emit(
                        session,
                        on_event,
                        ErrorEvent(
                            message=(
                                "Agent stuck in a loop — same tool calls repeated too many times"
                            ),
                            recoverable=False,
                        ),
                    )
                    session.state = AgentState.ERROR
                    return session

                # Emit ToolCall events BEFORE AssistantMessage so that
                # session.to_messages() sees the correct order:
                #   ToolCall… → AssistantMessage → ToolResult…
                # and can bundle the tool_calls onto the assistant message.
                for tc in response.tool_calls:
                    spec = tool_executor.registry.get(tc.tool_name)
                    if spec:
                        tc.data["side_effects"] = [e.value for e in spec.side_effects]
                    emit(session, on_event, tc)
                emit(
                    session,
                    on_event,
                    AssistantMessage(content=response.content, stop_reason=StopReason.TOOL_USE),
                )

                session.state = AgentState.WAITING_FOR_TOOL
                results = await tool_executor.execute(response.tool_calls)
                for tr in results:
                    emit(session, on_event, tr)
                session.state = AgentState.RUNNING
                continue

            stop = parse_stop(response.stop_reason)
            emit(session, on_event, AssistantMessage(content=response.content, stop_reason=stop))

            if stop == StopReason.MAX_TOKENS and guardrails.should_continue_after_max_tokens(
                session
            ):
                append_internal_user_message(
                    session,
                    on_event,
                    guardrails.max_tokens_followup(),
                    reason="max_tokens_recovery",
                )
                continue

            if stop in {StopReason.END_TURN, StopReason.MAX_TOKENS}:
                done = True

        if session.step_count >= config.max_steps and not done:
            session.state = AgentState.MAX_STEPS
            emit(
                session,
                on_event,
                ErrorEvent(message=f"Reached max steps ({config.max_steps})", recoverable=False),
            )

        if session.state == AgentState.RUNNING:
            session.state = AgentState.COMPLETED

    except asyncio.CancelledError:
        session.state = AgentState.CANCELLED
        emit(session, on_event, ErrorEvent(message="Agent cancelled", recoverable=False))
        raise

    return session
