from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent.extensions.api import BlockResult, ExtensionContext
from agent.extensions.loader import ExtensionInfo, discover_extensions, load_extension

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent.core.config import AgentConfig
    from agent.core.session import Session
    from agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class ExtensionManager:
    """Integrates loaded extensions with the agent runtime."""

    def __init__(self) -> None:
        self._extensions: list[ExtensionInfo] = []
        self._context: ExtensionContext | None = None

    async def initialize(
        self,
        session: Session,
        config: AgentConfig,
        cancel_event: asyncio.Event | None = None,
        *,
        user_dir: Path | None = None,
        project_dir: Path | None = None,
    ) -> None:
        """Load all extensions and create the shared context."""
        self._context = ExtensionContext(
            session=session,
            config=config,
            signal=cancel_event or asyncio.Event(),
            logger=logging.getLogger("aar.extensions"),
        )

        infos = discover_extensions(user_dir=user_dir, project_dir=project_dir)
        logger.info("Discovered %d extension(s)", len(infos))

        for info in infos:
            try:
                api = await load_extension(info)
                info.api = api
                logger.info("Loaded extension %r from %s", info.name, info.source)
            except Exception as exc:
                info.error = str(exc)
                logger.error("Failed to load extension %r: %s", info.name, exc)

        self._extensions = infos

    def register_tools(self, registry: ToolRegistry) -> int:
        """Register all extension-provided tools into the registry. Returns count."""
        count = 0
        for info in self._extensions:
            if info.api is None:
                continue
            for spec in info.api._tools:
                registry.add(spec)
                logger.debug("Registered tool %r from extension %r", spec.name, info.name)
                count += 1
        return count

    def update_session(self, session: Any) -> None:
        """Replace the session in the shared context with a live session object.

        Call this before dispatching slash-commands or after each agent.run()
        so that extension handlers see current session data rather than the
        bootstrap snapshot.
        """
        if self._context is None:
            return
        from dataclasses import replace

        self._context = replace(self._context, session=session)

    def get_system_prompt_additions(self) -> str:
        """Return all system prompt additions joined by newlines."""
        parts: list[str] = []
        for info in self._extensions:
            if info.api is None:
                continue
            parts.extend(info.api._system_prompt_parts)
        return "\n".join(parts)

    async def fire_event(self, event_name: str, event: Any) -> Any:
        """Fire event to all extension handlers.

        For tool_call: if any handler returns BlockResult, return it.
        For user_message / tool_result: if handler returns str, use it as
        replacement and pass the transformed value to subsequent handlers
        (pipeline semantics).
        """
        result: Any = None
        current_payload = event

        for info in self._extensions:
            if info.api is None:
                continue

            handlers = info.api._event_handlers.get(event_name, [])
            for handler in handlers:
                try:
                    if asyncio.iscoroutinefunction(handler):
                        rv = await handler(current_payload, self._context)
                    else:
                        rv = handler(current_payload, self._context)
                except Exception as exc:
                    logger.error(
                        "Error in %r handler from extension %r: %s",
                        event_name,
                        info.name,
                        exc,
                    )
                    continue

                if rv is None:
                    continue

                if event_name == "tool_call" and isinstance(rv, BlockResult):
                    logger.info("Extension %r blocked tool_call: %s", info.name, rv.reason)
                    return rv

                if event_name in ("user_message", "tool_result") and isinstance(rv, str):
                    result = rv
                    current_payload = rv  # pipeline: pass transformed value to next handler

            # Also fire on the extension's own event bus
            info.api.events.emit(event_name, current_payload)

        return result

    @property
    def commands(self) -> dict[str, tuple[str, Callable]]:
        """All registered slash-commands from all extensions."""
        merged: dict[str, tuple[str, Callable]] = {}
        for info in self._extensions:
            if info.api is None:
                continue
            merged.update(info.api._commands)
        return merged

    @property
    def loaded_extensions(self) -> list[ExtensionInfo]:
        return list(self._extensions)
