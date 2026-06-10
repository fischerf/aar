from __future__ import annotations

import asyncio

import pytest

from agent.transports.prompt_queue import PromptQueue, QueuedPrompt


class TestQueuedPrompt:
    def test_default_metadata_is_empty_dict(self):
        prompt = QueuedPrompt(content="hello")
        assert prompt.metadata == {}

    def test_stores_string_content(self):
        prompt = QueuedPrompt(content="do something")
        assert prompt.content == "do something"

    def test_stores_list_content(self):
        blocks = [{"type": "text", "text": "hello"}, {"type": "image", "url": "x.png"}]
        prompt = QueuedPrompt(content=blocks)
        assert prompt.content == blocks
        assert len(prompt.content) == 2


class TestPromptQueue:
    def test_enqueue_returns_incrementing_depth(self):
        q = PromptQueue()
        assert q.enqueue("first") == 1
        assert q.enqueue("second") == 2
        assert q.enqueue("third") == 3

    def test_peek_returns_first_item_without_removing(self):
        q = PromptQueue()
        q.enqueue("first")
        q.enqueue("second")
        item = q.peek()
        assert item is not None
        assert item.content == "first"
        # Still in queue
        assert q.depth == 2

    def test_pop_returns_and_removes_first_item_fifo(self):
        q = PromptQueue()
        q.enqueue("first")
        q.enqueue("second")
        item = q.pop()
        assert item is not None
        assert item.content == "first"
        assert q.depth == 1
        item = q.pop()
        assert item is not None
        assert item.content == "second"
        assert q.depth == 0

    def test_pop_on_empty_returns_none(self):
        q = PromptQueue()
        assert q.pop() is None

    def test_clear_returns_count_and_empties_queue(self):
        q = PromptQueue()
        q.enqueue("a")
        q.enqueue("b")
        q.enqueue("c")
        count = q.clear()
        assert count == 3
        assert q.is_empty
        assert q.depth == 0

    def test_clear_on_empty_returns_zero(self):
        q = PromptQueue()
        assert q.clear() == 0

    def test_depth_property(self):
        q = PromptQueue()
        assert q.depth == 0
        q.enqueue("x")
        assert q.depth == 1

    def test_is_empty_property(self):
        q = PromptQueue()
        assert q.is_empty is True
        q.enqueue("x")
        assert q.is_empty is False
        q.pop()
        assert q.is_empty is True


async def _stop_drain_task(q: PromptQueue, task: asyncio.Task) -> None:  # type: ignore[type-arg]
    """Helper: stop the drain loop and await the task regardless of state."""
    q.stop_drain()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class TestPromptQueueDrain:
    @pytest.mark.asyncio
    async def test_drain_dispatches_queued_prompts_when_idle(self):
        q = PromptQueue()
        dispatched: list[str] = []

        async def run_fn(content):
            dispatched.append(content)

        q.enqueue("hello")
        q.enqueue("world")

        task = asyncio.create_task(
            q.start_drain(
                run_fn=run_fn,
                is_idle_fn=lambda: True,
                on_dispatch=None,
                poll_interval_ms=10,
            )
        )
        await asyncio.sleep(0.05)
        await _stop_drain_task(q, task)

        assert dispatched == ["hello", "world"]

    @pytest.mark.asyncio
    async def test_drain_waits_when_not_idle_then_dispatches_once_idle(self):
        q = PromptQueue()
        dispatched: list[str] = []
        idle = False

        async def run_fn(content):
            dispatched.append(content)

        def is_idle_fn():
            return idle

        q.enqueue("deferred")

        task = asyncio.create_task(
            q.start_drain(
                run_fn=run_fn,
                is_idle_fn=is_idle_fn,
                on_dispatch=None,
                poll_interval_ms=10,
            )
        )
        # Give drain time to poll while not idle
        await asyncio.sleep(0.04)
        assert dispatched == []

        # Now become idle
        idle = True
        await asyncio.sleep(0.04)

        await _stop_drain_task(q, task)

        assert dispatched == ["deferred"]

    @pytest.mark.asyncio
    async def test_drain_calls_on_dispatch_callback(self):
        q = PromptQueue()
        callback_args: list[tuple[QueuedPrompt, int]] = []

        async def run_fn(content):
            pass

        def on_dispatch(prompt: QueuedPrompt, remaining: int):
            callback_args.append((prompt, remaining))

        q.enqueue("tracked")

        task = asyncio.create_task(
            q.start_drain(
                run_fn=run_fn,
                is_idle_fn=lambda: True,
                on_dispatch=on_dispatch,
                poll_interval_ms=10,
            )
        )
        await asyncio.sleep(0.05)
        await _stop_drain_task(q, task)

        assert len(callback_args) == 1
        assert callback_args[0][0].content == "tracked"
        assert callback_args[0][1] == 0  # no more items remaining

    @pytest.mark.asyncio
    async def test_drain_stops_when_stop_drain_called(self):
        q = PromptQueue()
        call_count = 0

        async def run_fn(content):
            nonlocal call_count
            call_count += 1

        q.enqueue("before stop")

        task = asyncio.create_task(
            q.start_drain(
                run_fn=run_fn,
                is_idle_fn=lambda: True,
                on_dispatch=None,
                poll_interval_ms=10,
            )
        )
        await asyncio.sleep(0.03)
        q.stop_drain()
        # Enqueue after stop — should not be dispatched
        q.enqueue("after stop")
        await asyncio.sleep(0.03)
        await _stop_drain_task(q, task)

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_drain_handles_empty_queue_without_crash(self):
        q = PromptQueue()

        async def run_fn(content):
            pass

        task = asyncio.create_task(
            q.start_drain(
                run_fn=run_fn,
                is_idle_fn=lambda: True,
                on_dispatch=None,
                poll_interval_ms=10,
            )
        )
        # Let it poll several times with nothing in the queue
        await asyncio.sleep(0.05)
        await _stop_drain_task(q, task)

        # If we get here without errors, the test passes
        assert q.is_empty

    @pytest.mark.asyncio
    async def test_drain_dispatches_multiple_prompts_in_fifo_order(self):
        q = PromptQueue()
        dispatched: list[str] = []

        async def run_fn(content):
            dispatched.append(content)
            # Simulate some work
            await asyncio.sleep(0.005)

        q.enqueue("alpha")
        q.enqueue("beta")
        q.enqueue("gamma")

        task = asyncio.create_task(
            q.start_drain(
                run_fn=run_fn,
                is_idle_fn=lambda: True,
                on_dispatch=None,
                poll_interval_ms=10,
            )
        )
        await asyncio.sleep(0.1)
        await _stop_drain_task(q, task)

        assert dispatched == ["alpha", "beta", "gamma"]
