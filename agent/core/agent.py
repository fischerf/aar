"""Agent — the main entry point that ties core, provider, and tools together."""

from __future__ import annotations

import logging
from typing import Any, Callable

from agent.core.config import AgentConfig, ProviderConfig
from agent.core.events import Event, SessionEvent
from agent.core.loop import run_loop
from agent.core.session import Session
from agent.core.state import AgentState
from agent.providers.base import Provider
from agent.tools.execution import ToolExecutor
from agent.tools.registry import ToolRegistry
from agent.tools.builtin.filesystem import register_filesystem_tools
from agent.tools.builtin.shell import register_shell_tools

logger = logging.getLogger(__name__)


PROVIDER_REGISTRY: dict[str, str] = {
    "anthropic": "agent.providers.anthropic.AnthropicProvider",
    "openai": "agent.providers.openai.OpenAIProvider",
    "ollama": "agent.providers.ollama.OllamaProvider",
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
    ) -> None:
        self.config = config or AgentConfig()
        self.provider = provider or _create_provider(self.config.provider)
        self.registry = registry or ToolRegistry()
        self.executor = ToolExecutor(
            self.registry, self.config.tools, self.config.safety
        )
        self._on_event: Callable[[Event], Any] | None = None

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
            register_shell_tools(self.registry)

        # Only prune builtins we just added that weren't explicitly enabled
        newly_added = set(self.registry.names()) - pre_existing
        for name in newly_added - enabled:
            if name in self.registry._tools:
                del self.registry._tools[name]

    def on_event(self, callback: Callable[[Event], Any]) -> None:
        """Set a callback that fires for every event during a run."""
        self._on_event = callback

    async def run(self, prompt: str, session: Session | None = None) -> Session:
        """Run the agent with a user prompt.

        Args:
            prompt: The user's message.
            session: Optional existing session to continue.

        Returns:
            The completed session.
        """
        if session is None:
            session = Session()
            session.append(SessionEvent(action="started"))

        session.add_user_message(prompt)
        session.state = AgentState.RUNNING

        session = await run_loop(
            session=session,
            provider=self.provider,
            tool_executor=self.executor,
            config=self.config,
            on_event=self._on_event,
        )

        return session

    async def chat(self, prompt: str, session: Session | None = None) -> str:
        """Convenience method: run and return just the final assistant text."""
        session = await self.run(prompt, session)
        # Find the last assistant message
        from agent.core.events import AssistantMessage
        for event in reversed(session.events):
            if isinstance(event, AssistantMessage) and event.content:
                return event.content
        return ""
