from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol, runtime_checkable

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

    def register_panel(self, panel: UIPanel) -> None: ...

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


# ---------------------------------------------------------------------------
# UI panels — a transport-agnostic contract for extensions that want a
# visual surface (a tree of nodes + actions) without shipping widget code.
#
# The extension describes *data* (a JSON-safe ``UINode`` tree) and *actions*
# (id, label, key, applicable node kinds, handler).  A transport that knows
# about panels (the fixed TUI, the ACP stdio agent) owns the presentation;
# transports that don't simply ignore ``ExtensionManager.panels``.
# ---------------------------------------------------------------------------


@dataclass
class UINode:
    """One row of a panel tree.

    ``id`` must be stable across snapshots (e.g. ``"cp:<sha>"``) — the TUI
    keys cursor position and expansion state on it.  ``kind`` selects which
    :class:`UIAction` entries apply; ``data`` is opaque to the transport and
    handed back to the action handler untouched.
    """

    id: str
    label: str
    kind: str = "info"
    children: list[UINode] = field(default_factory=list)
    expanded: bool = True
    style: str = ""  # theme role hint: "active" | "dim" | "warn" | ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe representation (used by the ACP transport)."""
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "expanded": self.expanded,
            "style": self.style,
            "data": dict(self.data),
            "children": [c.to_dict() for c in self.children],
        }

    def find(self, node_id: str) -> UINode | None:
        """Depth-first lookup by ``id``."""
        if self.id == node_id:
            return self
        for child in self.children:
            hit = child.find(node_id)
            if hit is not None:
                return hit
        return None


@dataclass
class UIInvocation:
    """What an action handler receives: the selected node, the extension
    context, and any extra arguments the UI collected (``force``, ``message``)."""

    node: UINode
    ctx: Any  # ExtensionContext
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class UIAction:
    """An operation the user can trigger on a node of a given kind.

    ``handler`` may be sync or async; sync handlers are run in a worker
    thread by :func:`run_ui_action` so a blocking implementation (git,
    subprocess) never stalls the transport's event loop.
    """

    id: str
    label: str
    key: str  # single key, e.g. "u" — only active while the panel has focus
    kinds: tuple[str, ...]  # node kinds this action applies to
    handler: Callable[[UIInvocation], str | None | Awaitable[str | None]]
    destructive: bool = False  # the transport must confirm first
    confirm: str = ""  # confirmation template; ``{label}`` is substituted
    inputs: tuple[str, ...] = ()  # extra args the UI should collect: "force", "message"
    mutates: bool = True  # refused while the agent is running

    def applies_to(self, node: UINode | None) -> bool:
        return node is not None and node.kind in self.kinds

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "key": self.key,
            "kinds": list(self.kinds),
            "destructive": self.destructive,
            "confirm": self.confirm,
            "inputs": list(self.inputs),
            "mutates": self.mutates,
        }


@dataclass
class UIPanel:
    """A panel an extension registers via :meth:`ExtensionAPI.register_panel`.

    ``snapshot(ctx)`` returns the current root :class:`UINode` (sync or
    async — see :func:`run_ui_snapshot`).  ``status(ctx)`` returns a short
    string for a header chip, or ``""`` to hide it.  The extension sets
    ``changed`` whenever its state moves; the transport refreshes and clears
    it.
    """

    name: str
    title: str
    snapshot: Callable[[Any], UINode | Awaitable[UINode]]
    actions: list[UIAction] = field(default_factory=list)
    status: Callable[[Any], str] | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def action(self, action_id: str) -> UIAction | None:
        for a in self.actions:
            if a.id == action_id:
                return a
        return None

    def actions_for(self, node: UINode | None) -> list[UIAction]:
        return [a for a in self.actions if a.applies_to(node)]

    def status_text(self, ctx: Any) -> str:
        if self.status is None:
            return ""
        try:
            return str(self.status(ctx) or "")
        except Exception as exc:  # never let a chip break the transport
            logger.debug("panel %r status() failed: %s", self.name, exc)
            return ""


async def _await_maybe_threaded(fn: Callable[..., Any], *args: Any) -> Any:
    """Call *fn*; await it if it is a coroutine function, otherwise run the
    blocking call in a worker thread."""
    if inspect.iscoroutinefunction(fn):
        return await fn(*args)
    result = await asyncio.to_thread(fn, *args)
    if inspect.isawaitable(result):
        return await result
    return result


async def run_ui_snapshot(panel: UIPanel, ctx: Any) -> UINode:
    """Produce *panel*'s current tree without blocking the event loop."""
    root = await _await_maybe_threaded(panel.snapshot, ctx)
    if not isinstance(root, UINode):
        raise TypeError(f"panel {panel.name!r} snapshot returned {type(root).__name__}")
    return root


async def run_ui_action(action: UIAction, invocation: UIInvocation) -> str | None:
    """Run *action* for *invocation*; returns the handler's message, if any."""
    result = await _await_maybe_threaded(action.handler, invocation)
    return None if result is None else str(result)


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
        self._panels: list[UIPanel] = []
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
    # UI panels
    # ------------------------------------------------------------------

    def register_panel(self, panel: UIPanel) -> None:
        """Register a :class:`UIPanel` for transports that can draw one.

        Panels are optional: transports without a visual surface ignore them,
        so an extension can register one unconditionally.
        """
        if any(p.name == panel.name for p in self._panels):
            logger.warning(
                "Extension %r registered panel %r twice — replacing", self.name, panel.name
            )
            self._panels = [p for p in self._panels if p.name != panel.name]
        self._panels.append(panel)
        logger.debug("Extension %r registered panel %r", self.name, panel.name)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def block(reason: str) -> BlockResult:
        """Convenience factory to create a :class:`BlockResult`."""
        return BlockResult(reason=reason)
