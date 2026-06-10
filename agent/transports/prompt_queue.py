"""Transport-agnostic prompt queue for auto-dispatching prompts when the agent is idle."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from agent.core.events import ContentBlock


@dataclass
class QueuedPrompt:
    """A prompt waiting to be dispatched."""

    content: str | list[ContentBlock]
    metadata: dict[str, Any] = field(default_factory=dict)


class PromptQueue:
    """Queue that stores prompts and auto-dispatches them when the agent becomes idle."""

    def __init__(self) -> None:
        self._queue: deque[QueuedPrompt] = deque()
        self._draining = False

    def enqueue(self, content: str | list[ContentBlock]) -> int:
        """Add a prompt to the queue. Returns the new queue depth."""
        self._queue.append(QueuedPrompt(content=content))
        return len(self._queue)

    def peek(self) -> QueuedPrompt | None:
        """Return the next prompt without removing it, or None if empty."""
        return self._queue[0] if self._queue else None

    def pop(self) -> QueuedPrompt | None:
        """Remove and return the next prompt, or None if empty."""
        return self._queue.popleft() if self._queue else None

    def clear(self) -> int:
        """Clear all queued prompts. Returns the count cleared."""
        count = len(self._queue)
        self._queue.clear()
        return count

    @property
    def depth(self) -> int:
        """Number of prompts currently queued."""
        return len(self._queue)

    @property
    def is_empty(self) -> bool:
        """Whether the queue has no prompts."""
        return len(self._queue) == 0

    async def start_drain(
        self,
        run_fn: Callable[[str | list[ContentBlock]], Awaitable[Any]],
        is_idle_fn: Callable[[], bool],
        on_dispatch: Callable[[QueuedPrompt, int], None] | None = None,
        poll_interval_ms: int = 100,
    ) -> None:
        """Poll for idle state and dispatch queued prompts until stopped or cancelled.

        Args:
            run_fn: Async callable that executes a prompt.
            is_idle_fn: Returns True when the agent is ready for a new prompt.
            on_dispatch: Optional callback invoked before dispatching (prompt, remaining depth).
            poll_interval_ms: How often to check idle state, in milliseconds.
        """
        self._draining = True
        interval = poll_interval_ms / 1000.0
        try:
            while self._draining:
                if is_idle_fn() and not self.is_empty:
                    prompt = self.pop()
                    assert prompt is not None  # guarded by is_empty check
                    if on_dispatch is not None:
                        on_dispatch(prompt, self.depth)
                    await run_fn(prompt.content)
                    # Always sleep after dispatch so the callee has time to
                    # mark the agent as busy before we poll idle again.
                    await asyncio.sleep(interval)
                else:
                    await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass
        finally:
            self._draining = False

    def stop_drain(self) -> None:
        """Signal the drain loop to stop after the current iteration."""
        self._draining = False
