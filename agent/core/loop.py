"""Thin core loop — the heart of the agent runtime."""

from __future__ import annotations

import asyncio
import logging
import time

from agent.core.config import AgentConfig
from agent.core.events import (
    AssistantMessage,
    ErrorEvent,
    StopReason,
)
from agent.core.session import Session
from agent.core.state import AgentState
from agent.providers.base import Provider, ProviderResponse
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

    _log = logger.getChild("loop")
    _extra = {"session_id": session.session_id, "trace_id": session.trace_id}

    try:
        while not done and session.step_count < config.max_steps:
            # Cooperative cancellation
            if cancel_event is not None and cancel_event.is_set():
                session.state = AgentState.CANCELLED
                _emit(session, on_event, ErrorEvent(message="Agent cancelled", recoverable=False))
                return session

            # Timeout check
            elapsed = time.monotonic() - start_time
            if elapsed > config.timeout:
                session.state = AgentState.ERROR
                _emit(session, on_event, ErrorEvent(
                    message=f"Agent timed out after {config.timeout}s",
                    recoverable=False,
                ))
                return session

            session.increment_step()
            messages = session.to_messages()
            tool_schemas = tool_executor.registry.to_provider_schemas() or None

            # Provider call — timed
            t_provider = time.monotonic()
            try:
                response: ProviderResponse = await provider.complete(
                    messages=messages,
                    tools=tool_schemas,
                    system=config.system_prompt,
                )
            except Exception as e:
                _log.exception(
                    "Provider error at step %d", session.step_count, extra=_extra
                )
                session.state = AgentState.ERROR
                _emit(session, on_event, ErrorEvent(
                    message=f"Provider error: {e}", recoverable=False
                ))
                return session

            provider_ms = (time.monotonic() - t_provider) * 1000

            # Stamp provider timing and record metadata
            if response.meta:
                response.meta.duration_ms = provider_ms
                _emit(session, on_event, response.meta)

            # Record reasoning blocks
            for rb in response.reasoning:
                _emit(session, on_event, rb)

            _log.info(
                "step=%d provider_ms=%.0f tool_calls=%d",
                session.step_count,
                provider_ms,
                len(response.tool_calls),
                extra=_extra,
            )

            # Handle tool calls
            if response.tool_calls:
                _emit(session, on_event, AssistantMessage(
                    content=response.content, stop_reason=StopReason.TOOL_USE
                ))
                for tc in response.tool_calls:
                    spec = tool_executor.registry.get(tc.tool_name)
                    if spec:
                        tc.data["side_effects"] = [e.value for e in spec.side_effects]
                    _emit(session, on_event, tc)

                session.state = AgentState.WAITING_FOR_TOOL
                results = await tool_executor.execute(response.tool_calls)
                for tr in results:
                    _emit(session, on_event, tr)
                session.state = AgentState.RUNNING
                continue

            # Final assistant message
            stop = _parse_stop(response.stop_reason)
            _emit(session, on_event, AssistantMessage(content=response.content, stop_reason=stop))
            if stop in {StopReason.END_TURN, StopReason.MAX_TOKENS}:
                done = True

        if session.step_count >= config.max_steps and not done:
            _emit(session, on_event, ErrorEvent(
                message=f"Reached max steps ({config.max_steps})", recoverable=False
            ))

        if session.state == AgentState.RUNNING:
            session.state = AgentState.COMPLETED

    except asyncio.CancelledError:
        session.state = AgentState.CANCELLED
        _emit(session, on_event, ErrorEvent(message="Agent cancelled", recoverable=False))
        raise

    return session


def _emit(session: Session, on_event, event) -> None:
    """Append an event to the session and fire the callback."""
    session.append(event)
    if on_event:
        on_event(event)


def _parse_stop(reason: str) -> StopReason:
    try:
        return StopReason(reason)
    except ValueError:
        return StopReason.END_TURN
