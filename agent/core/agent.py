"""Agent — the main entry point that ties core, provider, and tools together."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Callable

from agent.core.config import AgentConfig, ProviderConfig
from agent.core.events import ContentBlock, Event, SessionEvent
from agent.core.loop import run_loop
from agent.core.session import Session
from agent.core.state import AgentState
from agent.extensions.manager import ExtensionManager
from agent.providers.base import Provider
from agent.safety.permissions import ApprovalCallback
from agent.tools.builtin.filesystem import register_filesystem_tools
from agent.tools.builtin.shell import register_shell_tools
from agent.tools.execution import ToolExecutor
from agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


PROVIDER_REGISTRY: dict[str, str] = {
    "anthropic": "agent.providers.anthropic.AnthropicProvider",
    "openai": "agent.providers.openai.OpenAIProvider",
    "ollama": "agent.providers.ollama.OllamaProvider",
    "generic": "agent.providers.generic.GenericProvider",
    "gemini": "agent.providers.gemini.GeminiProvider",
}


def _create_provider(config: ProviderConfig) -> Provider:
    """Instantiate the appropriate provider from config."""
    class_path = PROVIDER_REGISTRY.get(config.name)
    if not class_path:
        available = ", ".join(sorted(PROVIDER_REGISTRY))
        raise ValueError(f"Unknown provider: '{config.name}'. Available: {available}")

    module_path, class_name = class_path.rsplit(".", 1)
    import importlib

    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    return cls(config)


class Agent:
    """High-level agent that owns config, provider, tools, and sessions."""

    def __init__(
        self,
        config: AgentConfig | None = None,
        provider: Provider | None = None,
        registry: ToolRegistry | None = None,
        approval_callback: ApprovalCallback | None = None,
    ) -> None:
        self.config = config or AgentConfig()
        self.provider = provider or _create_provider(self.config.resolve_provider())
        self.registry = registry or ToolRegistry()
        self.executor = ToolExecutor(
            self.registry,
            self.config.tools,
            self.config.safety,
            approval_callback,
        )
        self._on_event: list[Callable[[Event], Any]] = []
        self._extension_manager: ExtensionManager | None = None

        # Register built-in tools based on config
        self._register_builtins()

    def _register_builtins(self) -> None:
        enabled = set(self.config.tools.enabled_builtins)
        fs_tools = {"read_file", "write_file", "edit_file", "list_directory"}
        shell_tools = {"bash"}

        # Track pre-existing tools (e.g. MCP) so we don't prune them
        pre_existing = set(self.registry.names())

        if enabled & fs_tools:
            register_filesystem_tools(self.registry)
        if enabled & shell_tools:
            register_shell_tools(
                self.registry,
                sandbox=self.executor.sandbox,
                default_timeout=self.config.tools.bash_default_timeout,
            )

        # Only prune builtins we just added that weren't explicitly enabled
        newly_added = set(self.registry.names()) - pre_existing
        for name in newly_added - enabled:
            if name in self.registry._tools:
                del self.registry._tools[name]

    def on_event(self, callback: Callable[[Event], Any]) -> None:
        """Register a callback that fires for every event during a run.

        Multiple callbacks are supported; each is called in registration order.
        Both sync and async callables are accepted.
        """
        self._on_event.append(callback)

    def off_event(self, callback: Callable[[Event], Any]) -> None:
        """Remove a previously registered event callback."""
        self._on_event = [cb for cb in self._on_event if cb != callback]

    def switch_provider(self, key_or_spec: str | ProviderConfig) -> str:
        """Switch the active provider between turns.

        Args:
            key_or_spec: Either a key from ``config.providers`` or an
                ad-hoc ``ProviderConfig``.

        Returns:
            Human-readable description of the new provider,
            e.g. ``"anthropic/claude-sonnet-4-6"``.
        """
        if isinstance(key_or_spec, str):
            if key_or_spec in self.config.providers:
                cfg = self.config.providers[key_or_spec]
            elif "/" in key_or_spec:
                provider_name, model = key_or_spec.split("/", 1)
                cfg = ProviderConfig(name=provider_name, model=model)
            else:
                raise ValueError(
                    f"'{key_or_spec}' is not a known provider key and "
                    f"doesn't match 'provider/model' format. "
                    f"Available keys: "
                    f"{', '.join(sorted(self.config.providers)) or '(none)'}"
                )
        else:
            cfg = key_or_spec

        self.provider = _create_provider(cfg)
        return f"{cfg.name}/{cfg.model}"

    async def _init_extensions(
        self,
        session: Session,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        """Initialize the extension manager and register extension tools."""
        mgr = ExtensionManager()
        await mgr.initialize(session, self.config, cancel_event)

        # Register extension tools
        count = mgr.register_tools(self.registry)
        if count:
            logger.info("Registered %d extension tool(s)", count)

        # Append system prompt additions
        additions = mgr.get_system_prompt_additions()
        if additions:
            self.config.system_prompt = self.config.system_prompt + "\n---\n" + additions

        self._extension_manager = mgr

    async def run(
        self,
        prompt: str | list[ContentBlock],
        session: Session | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> Session:
        """Run the agent with a user prompt.

        Args:
            prompt: The user's message — either a plain string or a list of
                :class:`~agent.core.events.ContentBlock` objects for multimodal
                (text + image) input.
            session: Optional existing session to continue.
            cancel_event: Optional asyncio.Event; set it to request cooperative
                cancellation of the agent loop.

        Returns:
            The completed session.
        """
        if session is None:
            session = Session()
            session.append(SessionEvent(action="started"))

        session.add_user_message(prompt)
        session.state = AgentState.RUNNING

        def _dispatch(event: Event) -> None:
            for cb in self._on_event:
                try:
                    if inspect.iscoroutinefunction(cb):
                        asyncio.ensure_future(cb(event))
                    else:
                        cb(event)
                except Exception:
                    logger.exception("Event callback %r failed on %s", cb, event.type)

        if self._extension_manager is None:
            await self._init_extensions(session, cancel_event)

        # Keep the extension context in sync with the live session so that
        # extension slash-commands (e.g. /inspect) see current data.
        if self._extension_manager is not None:
            self._extension_manager.update_session(session)

        session = await run_loop(
            session=session,
            provider=self.provider,
            tool_executor=self.executor,
            config=self.config,
            on_event=_dispatch if self._on_event else None,
            cancel_event=cancel_event,
            extension_manager=self._extension_manager,
        )

        return session

    async def chat(
        self,
        prompt: str | list[ContentBlock],
        session: Session | None = None,
    ) -> str:
        """Convenience method: run and return just the final assistant text.

        Args:
            prompt: The user's message — either a plain string or a list of
                :class:`~agent.core.events.ContentBlock` objects for multimodal
                (text + image) input.
            session: Optional existing session to continue.

        Returns:
            The last assistant message text, or an empty string.
        """
        session = await self.run(prompt, session)
        # Find the last assistant message
        from agent.core.events import AssistantMessage

        for event in reversed(session.events):
            if isinstance(event, AssistantMessage) and event.content:
                return event.content
        return ""
