"""Event stream interface — transport-agnostic event bus."""

from __future__ import annotations

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
