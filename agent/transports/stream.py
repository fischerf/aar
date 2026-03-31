"""Event stream interface — transport-agnostic event bus."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from agent.core.events import Event


class EventStream:
    """Synchronous event bus for connecting the agent to any transport."""

    def __init__(self) -> None:
        self._listeners: list[Callable[[Event], Any]] = []

    def subscribe(self, listener: Callable[[Event], Any]) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: Callable[[Event], Any]) -> None:
        self._listeners.remove(listener)

    def emit(self, event: Event) -> None:
        for listener in self._listeners:
            listener(event)


class AsyncEventStream:
    """Async event stream backed by an asyncio.Queue."""

    def __init__(self, maxsize: int = 0) -> None:
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=maxsize)

    async def put(self, event: Event) -> None:
        await self._queue.put(event)

    def put_nowait(self, event: Event) -> None:
        self._queue.put_nowait(event)

    async def end(self) -> None:
        await self._queue.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self) -> Event:
        event = await self._queue.get()
        if event is None:
            raise StopAsyncIteration
        return event
