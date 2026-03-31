"""Thin core loop — the heart of the agent runtime."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator

from agent.core.config import AgentConfig
from agent.core.events import (
    AssistantMessage,
    ErrorEvent,
    StopReason,
    ToolCall,
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
) -> Session:
    """Run the agent loop until completion, max steps, or timeout.

    Args:
        session: The current session with conversation history.
        provider: The LLM provider to use.
        tool_executor: Executor for tool calls.
        config: Agent configuration.
        on_event: Optional callback called with each new event.

    Returns:
        The updated session.
    """
    session.state = AgentState.RUNNING
    start_time = time.monotonic()
    done = False

    while not done and session.step_count < config.max_steps:
        # Timeout check
        elapsed = time.monotonic() - start_time
        if elapsed > config.timeout:
            session.state = AgentState.ERROR
            err = ErrorEvent(
                message=f"Agent timed out after {config.timeout}s",
                recoverable=False,
            )
            session.append(err)
            if on_event:
                on_event(err)
            break

        session.increment_step()

        # Build messages and tool schemas
        messages = session.to_messages()
        tool_schemas = tool_executor.registry.to_provider_schemas() or None

        try:
            response: ProviderResponse = await provider.complete(
                messages=messages,
                tools=tool_schemas,
                system=config.system_prompt,
            )
        except Exception as e:
            logger.exception("Provider error at step %d", session.step_count)
            err = ErrorEvent(message=f"Provider error: {e}", recoverable=False)
            session.append(err)
            session.state = AgentState.ERROR
            if on_event:
                on_event(err)
            break

        # Record provider metadata
        if response.meta:
            session.append(response.meta)
            if on_event:
                on_event(response.meta)

        # Record reasoning blocks
        for rb in response.reasoning:
            session.append(rb)
            if on_event:
                on_event(rb)

        # Handle tool calls
        if response.tool_calls:
            # Record the assistant message (may have text + tool calls)
            assistant_msg = AssistantMessage(
                content=response.content,
                stop_reason=StopReason.TOOL_USE,
            )
            session.append(assistant_msg)
            if on_event:
                on_event(assistant_msg)

            for tc in response.tool_calls:
                session.append(tc)
                if on_event:
                    on_event(tc)

            # Execute tools
            session.state = AgentState.WAITING_FOR_TOOL
            results = await tool_executor.execute(response.tool_calls)
            for tr in results:
                session.append(tr)
                if on_event:
                    on_event(tr)

            session.state = AgentState.RUNNING
            continue

        # No tool calls — record the final assistant message
        stop = _parse_stop(response.stop_reason)
        assistant_msg = AssistantMessage(content=response.content, stop_reason=stop)
        session.append(assistant_msg)
        if on_event:
            on_event(assistant_msg)

        if stop in {StopReason.END_TURN, StopReason.MAX_TOKENS}:
            done = True

    if session.step_count >= config.max_steps and not done:
        err = ErrorEvent(message=f"Reached max steps ({config.max_steps})", recoverable=False)
        session.append(err)
        if on_event:
            on_event(err)

    if done:
        session.state = AgentState.COMPLETED
    elif session.state == AgentState.RUNNING:
        session.state = AgentState.COMPLETED

    return session


def _parse_stop(reason: str) -> StopReason:
    try:
        return StopReason(reason)
    except ValueError:
        return StopReason.END_TURN
