"""Integration tests for the prompt queue in the TUI Fixed transport.

Verifies that:
- Submitting while the agent is running enqueues the prompt.
- The drain loop auto-dispatches queued prompts when the agent becomes idle.
- Cancelling clears the queue.
- The _agent_running flag prevents drain-loop races.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.core.agent import Agent
from agent.core.config import AgentConfig
from agent.core.session import Session
from agent.core.state import AgentState
from agent.transports.prompt_queue import PromptQueue
from agent.transports.tui_fixed import AarFixedApp
from agent.transports.tui_widgets.bars import HeaderBar
from agent.transports.tui_widgets.input import HistoryTextArea

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_agent(run_delay: float = 0.0) -> Agent:
    """Create a mock Agent whose ``run`` takes *run_delay* seconds."""
    config = AgentConfig()
    provider = MagicMock()
    provider.name = "test"
    provider.model = "test-model"
    provider.supports_audio = False
    provider.supports_reasoning = False
    provider.supports_vision = False
    provider.config = MagicMock()
    provider.config.name = "test"
    provider.config.model = "test-model"
    registry = MagicMock()
    registry.names.return_value = []
    registry.list_tools.return_value = []
    registry.to_provider_schemas.return_value = None

    agent = MagicMock(spec=Agent)
    agent.config = config
    agent.provider = provider
    agent.registry = registry
    agent.on_event = MagicMock()
    agent._on_event = []

    async def fake_run(content, session=None, cancel_event=None):
        sess = session or Session()
        sess.state = AgentState.RUNNING
        if run_delay > 0:
            await asyncio.sleep(run_delay)
        sess.state = AgentState.COMPLETED
        return sess

    agent.run = AsyncMock(side_effect=fake_run)
    return agent


# ---------------------------------------------------------------------------
# Unit tests — verify queue logic directly (no Textual message loop needed)
# ---------------------------------------------------------------------------


class TestPromptQueueLogic:
    """Test the queue integration logic on AarFixedApp without relying on
    Textual's widget message dispatch (which requires careful pilot
    choreography).  These exercise the same code paths as real user input.
    """

    def test_agent_is_idle_when_not_running(self) -> None:
        agent = _make_mock_agent()
        app = AarFixedApp(agent=agent, config=AgentConfig())
        assert app._agent_is_idle() is True

    def test_agent_is_not_idle_while_running(self) -> None:
        agent = _make_mock_agent()
        app = AarFixedApp(agent=agent, config=AgentConfig())
        app._agent_running = True
        assert app._agent_is_idle() is False

    def test_agent_is_not_idle_with_running_session(self) -> None:
        agent = _make_mock_agent()
        app = AarFixedApp(agent=agent, config=AgentConfig())
        app._session = Session()
        app._session.state = AgentState.RUNNING
        assert app._agent_is_idle() is False

    def test_agent_is_idle_with_completed_session(self) -> None:
        agent = _make_mock_agent()
        app = AarFixedApp(agent=agent, config=AgentConfig())
        app._session = Session()
        app._session.state = AgentState.COMPLETED
        assert app._agent_is_idle() is True

    def test_running_flag_takes_precedence(self) -> None:
        """Even if session.state is COMPLETED, _agent_running=True means busy."""
        agent = _make_mock_agent()
        app = AarFixedApp(agent=agent, config=AgentConfig())
        app._session = Session()
        app._session.state = AgentState.COMPLETED
        app._agent_running = True
        assert app._agent_is_idle() is False

    def test_restore_input_clears_running_flag(self) -> None:
        agent = _make_mock_agent()
        app = AarFixedApp(agent=agent, config=AgentConfig())
        app._agent_running = True
        # _restore_input accesses widgets — but it wraps in try/except,
        # so calling it without a mounted app just skips the widget work.
        app._restore_input()
        assert app._agent_running is False


class TestPromptQueueDrainIntegration:
    """Test the drain loop with the real _agent_is_idle / _agent_running."""

    @pytest.mark.asyncio
    async def test_drain_respects_running_flag(self) -> None:
        """Drain loop must NOT dispatch while _agent_running is True."""
        q = PromptQueue()
        running = True

        dispatched: list[str] = []

        async def run_fn(content):
            dispatched.append(content)

        def is_idle():
            return not running

        q.enqueue("should-wait")

        task = asyncio.create_task(
            q.start_drain(run_fn=run_fn, is_idle_fn=is_idle, poll_interval_ms=10)
        )
        await asyncio.sleep(0.05)
        assert dispatched == [], "Should not dispatch while running=True"

        running = False
        await asyncio.sleep(0.05)
        assert dispatched == ["should-wait"]

        q.stop_drain()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_drain_sequential_dispatch(self) -> None:
        """Drain loop dispatches one at a time, waiting for idle between each."""
        q = PromptQueue()
        running = False
        dispatched: list[str] = []

        async def run_fn(content):
            nonlocal running
            dispatched.append(content)
            running = True
            await asyncio.sleep(0.05)
            running = False

        q.enqueue("a")
        q.enqueue("b")
        q.enqueue("c")

        task = asyncio.create_task(
            q.start_drain(run_fn=run_fn, is_idle_fn=lambda: not running, poll_interval_ms=10)
        )
        await asyncio.sleep(0.5)

        q.stop_drain()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert dispatched == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Full Textual integration tests (widget-level)
# ---------------------------------------------------------------------------


class TestPromptQueueTuiFixed:
    @pytest.mark.asyncio
    async def test_input_not_disabled_while_agent_runs(self) -> None:
        """The input widget must remain enabled so users can queue prompts."""
        agent = _make_mock_agent(run_delay=5.0)
        app = AarFixedApp(agent=agent, config=AgentConfig())

        async with app.run_test(size=(120, 40)) as pilot:
            inp = app.query_one("#user-input", HistoryTextArea)
            inp.focus()
            await pilot.pause()

            # Type a message and submit via keypress
            inp.text = "first prompt"
            await pilot.press("ctrl+s")
            await pilot.pause()
            await asyncio.sleep(0.1)

            # Input should NOT be disabled — users need to type queued prompts
            assert not inp.disabled, "Input must stay enabled for queuing"

    @pytest.mark.asyncio
    async def test_submit_while_busy_enqueues(self) -> None:
        """Submitting while the agent is busy should enqueue, not run."""
        agent = _make_mock_agent(run_delay=5.0)
        app = AarFixedApp(agent=agent, config=AgentConfig())

        async with app.run_test(size=(120, 40)) as pilot:
            inp = app.query_one("#user-input", HistoryTextArea)
            inp.focus()
            await pilot.pause()

            # First submit — starts the agent
            inp.text = "first"
            await pilot.press("ctrl+s")
            await pilot.pause()
            await asyncio.sleep(0.1)

            # Verify first submit worked
            assert agent.run.call_count == 1, "First submit should have triggered agent.run"
            assert app._agent_running is True, "Agent should still be running"

            # Second submit while busy — should enqueue
            inp.text = "second"
            await pilot.pause()  # let text update propagate
            await pilot.press("ctrl+s")
            await pilot.pause()
            await asyncio.sleep(0.05)

            assert app._prompt_queue.depth == 1
            peeked = app._prompt_queue.peek()
            assert peeked is not None
            assert peeked.content == "second"
            # Agent.run should have been called only once (for "first")
            assert agent.run.call_count == 1

    @pytest.mark.asyncio
    async def test_drain_auto_dispatches_queued(self) -> None:
        """Queued prompts should auto-dispatch when the agent becomes idle."""
        agent = _make_mock_agent(run_delay=0.15)
        app = AarFixedApp(agent=agent, config=AgentConfig())

        async with app.run_test(size=(120, 40)) as pilot:
            inp = app.query_one("#user-input", HistoryTextArea)
            inp.focus()
            await pilot.pause()

            # First submit
            inp.text = "first"
            await pilot.press("ctrl+s")
            await pilot.pause()
            await asyncio.sleep(0.1)

            # Enqueue while busy
            inp.text = "second"
            await pilot.press("ctrl+s")
            await pilot.pause()

            # Wait for first run + drain to dispatch second
            await asyncio.sleep(0.8)
            await pilot.pause()

            assert app._prompt_queue.is_empty
            assert agent.run.call_count == 2

    @pytest.mark.asyncio
    async def test_cancel_clears_queue(self) -> None:
        """Ctrl+X should cancel the running agent AND clear the queue."""
        agent = _make_mock_agent(run_delay=5.0)
        app = AarFixedApp(agent=agent, config=AgentConfig())

        async with app.run_test(size=(120, 40)) as pilot:
            inp = app.query_one("#user-input", HistoryTextArea)
            inp.focus()
            await pilot.pause()

            # Start agent
            inp.text = "first"
            await pilot.press("ctrl+s")
            await pilot.pause()
            await asyncio.sleep(0.1)

            # Enqueue two more
            inp.text = "queued1"
            await pilot.press("ctrl+s")
            await pilot.pause()
            inp.text = "queued2"
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert app._prompt_queue.depth == 2

            # Cancel
            await app.run_action("cancel_agent")
            await pilot.pause()

            assert app._prompt_queue.is_empty
            assert app._agent_running is False

    @pytest.mark.asyncio
    async def test_header_shows_queue_depth(self) -> None:
        """Header bar should reflect queue depth."""
        agent = _make_mock_agent(run_delay=5.0)
        app = AarFixedApp(agent=agent, config=AgentConfig())

        async with app.run_test(size=(120, 40)) as pilot:
            inp = app.query_one("#user-input", HistoryTextArea)
            header = app.query_one(HeaderBar)
            inp.focus()
            await pilot.pause()

            assert header.queue_depth == 0

            # Start agent + enqueue
            inp.text = "first"
            await pilot.press("ctrl+s")
            await pilot.pause()
            await asyncio.sleep(0.1)

            inp.text = "queued"
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert header.queue_depth == 1
