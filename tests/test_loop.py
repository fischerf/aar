"""Core loop tests — termination, tool handling, timeout, cancellation, errors."""

from __future__ import annotations

import asyncio

import pytest

from agent.core.config import AgentConfig, ProviderConfig, SafetyConfig, ToolConfig
from agent.core.events import (
    AssistantMessage,
    ErrorEvent,
    EventType,
    ProviderMeta,
    ReasoningBlock,
    StopReason,
    ToolCall,
    ToolResult,
)
from agent.core.loop import run_loop
from agent.core.session import Session
from agent.core.state import AgentState
from agent.providers.base import ProviderResponse
from agent.tools.execution import ToolExecutor

from tests.conftest import MockProvider


# ---------------------------------------------------------------------------
# Loop termination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_terminates_on_end_turn(mock_provider, tool_registry, default_config):
    """Loop should stop when provider returns end_turn stop reason."""
    mock_provider.enqueue_text("Hello!", stop="end_turn")
    session = Session()
    session.add_user_message("Hi")

    executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
    result = await run_loop(session, mock_provider, executor, default_config)

    assert result.state == AgentState.COMPLETED
    assert result.step_count == 1
    assistant_msgs = [e for e in result.events if isinstance(e, AssistantMessage)]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0].content == "Hello!"


@pytest.mark.asyncio
async def test_loop_terminates_on_max_tokens(mock_provider, tool_registry, default_config):
    """Loop should stop when provider returns max_tokens."""
    mock_provider.enqueue_text("Truncated response", stop="max_tokens")
    session = Session()
    session.add_user_message("Tell me a long story")

    executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
    result = await run_loop(session, mock_provider, executor, default_config)

    assert result.state == AgentState.COMPLETED
    assistant_msgs = [e for e in result.events if isinstance(e, AssistantMessage)]
    assert assistant_msgs[0].stop_reason == StopReason.MAX_TOKENS


@pytest.mark.asyncio
async def test_loop_enforces_max_steps(mock_provider, tool_registry):
    """Loop should stop after max_steps even if provider keeps going."""
    config = AgentConfig(
        provider=ProviderConfig(name="mock"),
        max_steps=3,
        timeout=30.0,
    )
    # Enqueue tool calls that never resolve to a final answer
    for i in range(5):
        mock_provider.enqueue_tool_call("echo", {"message": f"step {i}"}, f"tc_{i}")
    mock_provider.enqueue_text("final")

    session = Session()
    session.add_user_message("Loop forever")

    executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
    result = await run_loop(session, mock_provider, executor, config)

    assert result.step_count == 3
    errors = [e for e in result.events if isinstance(e, ErrorEvent)]
    assert any("max steps" in e.message.lower() for e in errors)


@pytest.mark.asyncio
async def test_loop_enforces_timeout(tool_registry):
    """Loop should stop when timeout is exceeded.

    The timeout is checked at the top of each iteration. We make the
    provider slow (0.15s) so that after the first call completes,
    elapsed > timeout on the second iteration's check.
    We queue tool-call responses (not end_turn) so the loop doesn't
    exit on the first step.
    """
    from tests.conftest import MockProvider

    class SlowMockProvider(MockProvider):
        async def complete(self, messages, tools=None, system=""):
            await asyncio.sleep(0.15)  # Each call takes 150ms
            return await super().complete(messages, tools, system)

    provider = SlowMockProvider()
    # Queue tool calls so the loop continues (no end_turn to stop it)
    for i in range(10):
        provider.enqueue_tool_call("echo", {"message": f"step {i}"}, f"tc_{i}")

    config = AgentConfig(
        provider=ProviderConfig(name="mock"),
        max_steps=100,
        timeout=0.05,  # 50ms — fires on second iteration check (after 150ms elapsed)
    )

    session = Session()
    session.add_user_message("Hi")

    executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
    result = await run_loop(session, provider, executor, config)

    assert result.state == AgentState.ERROR
    errors = [e for e in result.events if isinstance(e, ErrorEvent)]
    assert any("timed out" in e.message.lower() for e in errors)


# ---------------------------------------------------------------------------
# Tool-call handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_executes_tool_call(mock_provider, tool_registry, default_config):
    """Loop should execute tool calls and continue with the result."""
    # Step 1: provider requests a tool call
    mock_provider.enqueue_tool_call("echo", {"message": "test"}, "tc_1")
    # Step 2: after seeing the result, provider gives final answer
    mock_provider.enqueue_text("Done: echo: test")

    session = Session()
    session.add_user_message("Call echo")

    executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
    result = await run_loop(session, mock_provider, executor, default_config)

    assert result.state == AgentState.COMPLETED
    assert result.step_count == 2

    # Verify tool call and result events exist
    tool_calls = [e for e in result.events if isinstance(e, ToolCall)]
    tool_results = [e for e in result.events if isinstance(e, ToolResult)]
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_name == "echo"
    assert len(tool_results) == 1
    assert tool_results[0].output == "echo: test"
    assert not tool_results[0].is_error


@pytest.mark.asyncio
async def test_loop_handles_unknown_tool(mock_provider, tool_registry, default_config):
    """Unknown tool calls should produce an error result, not crash."""
    mock_provider.enqueue_tool_call("nonexistent_tool", {"x": 1}, "tc_1")
    mock_provider.enqueue_text("Tool failed")

    session = Session()
    session.add_user_message("Call unknown tool")

    executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
    result = await run_loop(session, mock_provider, executor, default_config)

    tool_results = [e for e in result.events if isinstance(e, ToolResult)]
    assert len(tool_results) == 1
    assert tool_results[0].is_error
    assert "unknown tool" in tool_results[0].output.lower()


@pytest.mark.asyncio
async def test_loop_handles_multiple_tool_calls(mock_provider, tool_registry, default_config):
    """Loop should handle multiple tool calls in a single response."""
    mock_provider._responses.append(
        ProviderResponse(
            content="Calling two tools",
            tool_calls=[
                ToolCall(tool_name="echo", tool_call_id="tc_1", arguments={"message": "first"}),
                ToolCall(tool_name="echo", tool_call_id="tc_2", arguments={"message": "second"}),
            ],
            stop_reason="tool_use",
            meta=ProviderMeta(provider="mock", model="mock-1", usage={"input_tokens": 10, "output_tokens": 20}),
        )
    )
    mock_provider.enqueue_text("Got both results")

    session = Session()
    session.add_user_message("Call two tools")

    executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
    result = await run_loop(session, mock_provider, executor, default_config)

    tool_results = [e for e in result.events if isinstance(e, ToolResult)]
    assert len(tool_results) == 2
    assert tool_results[0].output == "echo: first"
    assert tool_results[1].output == "echo: second"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_handles_provider_error(error_provider, tool_registry, default_config):
    """Provider errors should be caught and recorded, not crash."""
    session = Session()
    session.add_user_message("Hi")

    executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
    result = await run_loop(session, error_provider, executor, default_config)

    assert result.state == AgentState.ERROR
    errors = [e for e in result.events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    assert "Provider error" in errors[0].message


@pytest.mark.asyncio
async def test_loop_handles_tool_execution_error(
    mock_provider, failing_tool_registry, default_config
):
    """Tool execution errors should produce error results, not crash the loop."""
    mock_provider.enqueue_tool_call("bad_tool", {}, "tc_1")
    mock_provider.enqueue_text("Tool error handled")

    session = Session()
    session.add_user_message("Run bad tool")

    executor = ToolExecutor(failing_tool_registry, ToolConfig(), SafetyConfig())
    result = await run_loop(session, mock_provider, executor, default_config)

    assert result.state == AgentState.COMPLETED
    tool_results = [e for e in result.events if isinstance(e, ToolResult)]
    assert len(tool_results) == 1
    assert tool_results[0].is_error
    assert "something went wrong" in tool_results[0].output.lower()


# ---------------------------------------------------------------------------
# Reasoning blocks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_records_reasoning_blocks(mock_provider, tool_registry, default_config):
    """Reasoning blocks from the provider should be recorded in the session."""
    mock_provider._responses.append(
        ProviderResponse(
            content="The answer is 42",
            stop_reason="end_turn",
            reasoning=[ReasoningBlock(content="Let me think about this...")],
            meta=ProviderMeta(provider="mock", model="mock-1", usage={"input_tokens": 10, "output_tokens": 5}),
        )
    )

    session = Session()
    session.add_user_message("What is the answer?")

    executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
    result = await run_loop(session, mock_provider, executor, default_config)

    reasoning = [e for e in result.events if isinstance(e, ReasoningBlock)]
    assert len(reasoning) == 1
    assert "think about this" in reasoning[0].content


# ---------------------------------------------------------------------------
# Event callback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_fires_event_callback(mock_provider, tool_registry, default_config):
    """The on_event callback should fire for every event."""
    mock_provider.enqueue_tool_call("echo", {"message": "hi"}, "tc_1")
    mock_provider.enqueue_text("Done")

    collected: list = []
    session = Session()
    session.add_user_message("Test")

    executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
    await run_loop(session, mock_provider, executor, default_config, on_event=collected.append)

    types = [e.type for e in collected]
    # Should have: provider_meta, assistant_message (tool_use), tool_call, tool_result,
    #              provider_meta, assistant_message (end_turn)
    assert EventType.PROVIDER_META in types
    assert EventType.ASSISTANT_MESSAGE in types
    assert EventType.TOOL_CALL in types
    assert EventType.TOOL_RESULT in types


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_respects_cancel_event(mock_provider, tool_registry, default_config):
    """Setting cancel_event before the loop starts should cancel immediately."""
    mock_provider.enqueue_text("Should not be reached")

    session = Session()
    session.add_user_message("Run forever")

    cancel_event = asyncio.Event()
    cancel_event.set()  # already set before the loop starts

    executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
    result = await run_loop(session, mock_provider, executor, default_config, cancel_event=cancel_event)

    assert result.state == AgentState.CANCELLED
    errors = [e for e in result.events if isinstance(e, ErrorEvent)]
    assert any("cancelled" in e.message.lower() for e in errors)
    # Provider should never have been called
    assert len(mock_provider.call_history) == 0


@pytest.mark.asyncio
async def test_loop_cancel_event_mid_run(tool_registry, default_config):
    """Setting cancel_event between steps should stop the loop cleanly."""
    cancel_event = asyncio.Event()

    class CancellingProvider(MockProvider):
        async def complete(self, messages, tools=None, system=""):
            # Cancel after the first call
            cancel_event.set()
            return await super().complete(messages, tools, system)

    provider = CancellingProvider()
    # First step: tool call so the loop continues; cancel fires, second step is skipped
    provider.enqueue_tool_call("echo", {"message": "hi"}, "tc_1")
    provider.enqueue_text("Should not be reached")

    session = Session()
    session.add_user_message("Go")

    executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
    result = await run_loop(
        session, provider, executor, default_config, cancel_event=cancel_event
    )

    assert result.state == AgentState.CANCELLED


@pytest.mark.asyncio
async def test_loop_handles_asyncio_cancelled_error(tool_registry, default_config):
    """asyncio task cancellation should set state to CANCELLED and re-raise."""
    class SlowProvider(MockProvider):
        async def complete(self, messages, tools=None, system=""):
            await asyncio.sleep(10)  # will be cancelled here
            return await super().complete(messages, tools, system)

    provider = SlowProvider()
    provider.enqueue_text("unreachable")

    session = Session()
    session.add_user_message("Hi")

    executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())

    task = asyncio.create_task(run_loop(session, provider, executor, default_config))
    await asyncio.sleep(0.01)  # let the task start
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.state == AgentState.CANCELLED


# ---------------------------------------------------------------------------
# Observability — timing fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_meta_has_duration(mock_provider, tool_registry, default_config):
    """ProviderMeta events should carry a non-negative duration_ms after the loop."""
    mock_provider.enqueue_text("Hi")

    session = Session()
    session.add_user_message("Hello")

    executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
    result = await run_loop(session, mock_provider, executor, default_config)

    metas = [e for e in result.events if isinstance(e, ProviderMeta)]
    assert len(metas) == 1
    assert metas[0].duration_ms >= 0.0


@pytest.mark.asyncio
async def test_tool_result_has_duration(mock_provider, tool_registry, default_config):
    """ToolResult events should carry a non-negative duration_ms."""
    mock_provider.enqueue_tool_call("echo", {"message": "ping"}, "tc_1")
    mock_provider.enqueue_text("Done")

    session = Session()
    session.add_user_message("Call echo")

    executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
    result = await run_loop(session, mock_provider, executor, default_config)

    tool_results = [e for e in result.events if isinstance(e, ToolResult)]
    assert len(tool_results) == 1
    assert tool_results[0].duration_ms >= 0.0
