from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

from agent.tools.schema import SideEffect, ToolSpec

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def _log_task_exception(task: asyncio.Task) -> None:
    """Log exceptions from fire-and-forget extension handler tasks."""
    if not task.cancelled() and task.exception() is not None:
        logger.error(
            "Async extension handler raised: %s", task.exception(), exc_info=task.exception()
        )


# ---------------------------------------------------------------------------
# Protocols — usable by third-party extensions for static type-checking
# without importing concrete Aar classes.
# ---------------------------------------------------------------------------


@runtime_checkable
class ExtensionContextProtocol(Protocol):
    """Protocol for type-checking extension context without importing Aar."""

    @property
    def session(self) -> Any: ...

    @property
    def config(self) -> Any: ...

    @property
    def signal(self) -> asyncio.Event: ...

    @property
    def logger(self) -> logging.Logger: ...


@runtime_checkable
class ExtensionAPIProtocol(Protocol):
    """Protocol for type-checking the extension API handle without importing Aar."""

    name: str
    events: Any  # ExtensionEventBus

    def on(self, event: str) -> Callable: ...

    def tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        *,
        side_effects: list[Any] | None = ...,
        requires_approval: bool = ...,
    ) -> Callable: ...

    def register_tool(self, spec: Any) -> None: ...

    def command(self, name: str, *, description: str = ...) -> Callable: ...

    def append_system_prompt(self, text: str) -> None: ...

    @staticmethod
    def block(reason: str) -> BlockResult: ...


@dataclass(frozen=True)
class BlockResult:
    """Returned by event handlers to block an action (e.g. a tool call)."""

    reason: str


@dataclass(frozen=True)
class ExtensionContext:
    """Read-only context handed to extensions at runtime."""

    session: Any  # Session object — kept as Any to avoid circular imports
    config: Any  # AgentConfig object
    signal: asyncio.Event  # cancel signal
    logger: logging.Logger  # scoped logger for the extension


class ExtensionEventBus:
    """Simple synchronous + async pub/sub bus scoped to a single extension."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable]] = defaultdict(list)

    def on(self, event: str) -> Callable:
        """Decorator to subscribe a handler to *event*."""

        def decorator(fn: Callable) -> Callable:
            self._handlers[event].append(fn)
            return fn

        return decorator

    def emit(self, event: str, payload: Any = None) -> None:
        """Fire *event* synchronously — async handlers are scheduled but not awaited."""
        for handler in self._handlers.get(event, []):
            try:
                result = handler(payload)
                if asyncio.iscoroutine(result):
                    # Best-effort: schedule on running loop if available.
                    try:
                        loop = asyncio.get_running_loop()
                        task = loop.create_task(result)
                        task.add_done_callback(_log_task_exception)
                    except RuntimeError:
                        # No running loop — discard the coroutine to avoid warnings.
                        result.close()
            except Exception:
                logger.exception("EventBus handler error for %s", event)

    async def emit_async(self, event: str, payload: Any = None) -> None:
        """Fire *event* and ``await`` any async handlers."""
        for handler in self._handlers.get(event, []):
            try:
                result = handler(payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("EventBus async handler error for %s", event)


# Valid lifecycle / hook event names that extensions can subscribe to.
_VALID_EVENTS: set[str] = {
    "session_start",
    "session_end",
    "before_turn",
    "after_turn",
    "user_message",
    "tool_call",
    "tool_result",
    "assistant_message",
    "stream_chunk",
    "error",
}


class ExtensionAPI:
    """Handle object given to an extension's ``register()`` function.

    Extensions use this to declare tools, commands, event hooks, and system-prompt
    additions.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._event_handlers: dict[str, list[Callable]] = defaultdict(list)
        self._tools: list[ToolSpec] = []
        self._commands: dict[str, tuple[str, Callable]] = {}
        self._system_prompt_parts: list[str] = []
        self.events = ExtensionEventBus()

    # ------------------------------------------------------------------
    # Event hooks
    # ------------------------------------------------------------------

    def on(self, event: str) -> Callable:
        """Decorator to register a lifecycle event handler.

        Supported events: ``session_start``, ``session_end``, ``before_turn``,
        ``after_turn``, ``user_message``, ``tool_call``, ``tool_result``,
        ``assistant_message``, ``stream_chunk``, ``error``.
        """

        if event not in _VALID_EVENTS:
            logger.warning("Extension %r registered handler for unknown event %r", self.name, event)

        def decorator(fn: Callable) -> Callable:
            self._event_handlers[event].append(fn)
            return fn

        return decorator

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        *,
        side_effects: list[SideEffect] | None = None,
        requires_approval: bool = False,
    ) -> Callable:
        """Decorator to register a tool provided by this extension."""

        def decorator(fn: Callable) -> Callable:
            spec = ToolSpec(
                name=name,
                description=description,
                input_schema=input_schema,
                side_effects=side_effects or [SideEffect.NONE],
                requires_approval=requires_approval,
                handler=fn,
            )
            self._tools.append(spec)
            logger.debug("Extension %r registered tool %r", self.name, name)
            return fn

        return decorator

    def register_tool(self, spec: ToolSpec) -> None:
        """Imperative alternative to the :pymethod:`tool` decorator."""
        self._tools.append(spec)
        logger.debug("Extension %r registered tool %r (imperative)", self.name, spec.name)

    # ------------------------------------------------------------------
    # Slash-commands
    # ------------------------------------------------------------------

    def command(self, name: str, *, description: str = "") -> Callable:
        """Decorator to register a slash-command (e.g. ``/mycmd``)."""

        def decorator(fn: Callable) -> Callable:
            self._commands[name] = (description, fn)
            logger.debug("Extension %r registered command /%s", self.name, name)
            return fn

        return decorator

    # ------------------------------------------------------------------
    # System prompt
    # ------------------------------------------------------------------

    def append_system_prompt(self, text: str) -> None:
        """Append *text* to the system prompt assembled for every turn."""
        self._system_prompt_parts.append(text)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def block(reason: str) -> BlockResult:
        """Convenience factory to create a :class:`BlockResult`."""
        return BlockResult(reason=reason)
