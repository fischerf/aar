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


async def _safe_async_callback(cb: Callable[[Event], Any], event: Event) -> None:
    """#7 — Run an async event callback and log any exception it raises.

    Without this wrapper, ``asyncio.ensure_future(cb(event))`` produces a Task
    whose exception is only surfaced when the task is awaited or its result
    accessed. We never await event-callback tasks (fire-and-forget by design),
    so exceptions previously vanished except for an asyncio "Task exception
    was never retrieved" warning at GC time.
    """
    try:
        await cb(event)
    except Exception:
        logger.exception("Async event callback %r failed on %s", cb, event.type)


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
        # #7 — Track pending async event callbacks. ``asyncio.ensure_future``
        # on a coroutine returns a Task that the event loop does NOT keep a
        # strong reference to (per Python 3.11+ asyncio docs), so without a
        # local strong ref the task can be GC'd mid-execution and silently
        # dropped. The plan called for a ``WeakSet`` here, but that has the
        # same problem — we keep a regular ``set`` and ``add_done_callback``
        # the discard so completed tasks self-clean.
        self._on_event_tasks: set[asyncio.Task[Any]] = set()
        self._extension_manager: ExtensionManager | None = None

        # Register built-in tools based on config
        self._register_builtins()

        # Build tool-aware system prompt
        self._rebuild_system_prompt()

    def _register_builtins(self) -> None:
        from agent.tools.builtin.search import register_search_tools

        enabled = set(self.config.tools.enabled_builtins)
        fs_tools = {"read_file", "write_file", "edit_file", "list_directory"}
        shell_tools = {"bash"}
        search_tools = {"grep", "find_files"}

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
        if enabled & search_tools:
            register_search_tools(self.registry)

        # Only prune builtins we just added that weren't explicitly enabled
        newly_added = set(self.registry.names()) - pre_existing
        for name in newly_added - enabled:
            self.registry.unregister(name)

    def _rebuild_system_prompt(self) -> None:
        """Rebuild the system prompt with current tool snippets, guidelines, and skills."""
        from agent.core.config import build_system_prompt

        # Load skills if enabled
        skills_text: str | None = None
        skill_read_globs: list[str] = []
        if self.config.skills_enabled:
            from agent.core.skills import (
                format_skills_for_prompt,
                load_skills,
                skill_read_paths,
            )

            result = load_skills(
                project_rules_dir=self.config.project_rules_dir,
                extra_dirs=self.config.skills_dirs or None,
            )
            if result.skills:
                skills_text = format_skills_for_prompt(result.skills)
                skill_read_globs = skill_read_paths(result.skills)

        # Grant the policy read-only access to discovered skill files so the
        # model can load skill instructions even when allowed_paths restricts it
        # to the workspace. Reassigned (not appended) each rebuild to stay
        # idempotent across repeated calls (e.g. after extensions register).
        self.executor.policy.config.read_only_paths = skill_read_globs

        sb = self.config.safety.sandbox
        self.config.system_prompt = build_system_prompt(
            project_rules_dir=self.config.project_rules_dir,
            sandbox_mode=sb.mode,
            wsl_distro=sb.wsl.distro,
            system_prompt_hint=sb.wsl.system_prompt_hint,
            tool_snippets=self.registry.get_prompt_snippets() or None,
            tool_guidelines=self.registry.get_prompt_guidelines() or None,
            skills_text=skills_text,
        )

    def on_event(self, callback: Callable[[Event], Any]) -> None:
        """Register a callback that fires for every event during a run.

        Multiple callbacks are supported; each is called in registration order.
        Both sync and async callables are accepted.
        """
        self._on_event.append(callback)

    def off_event(self, callback: Callable[[Event], Any]) -> None:
        """Remove a previously registered event callback."""
        self._on_event = [cb for cb in self._on_event if cb != callback]

    def switch_provider(
        self, key_or_spec: str | ProviderConfig, session: Session | None = None
    ) -> str:
        """Switch the active provider between turns.

        Args:
            key_or_spec: Either a key from ``config.providers`` or an
                ad-hoc ``ProviderConfig``.
            session: Optional session to emit a :class:`ProviderSwitchEvent` into.

        Returns:
            Human-readable description of the new provider,
            e.g. ``"anthropic/claude-sonnet-4-6"``.
        """
        old_name = self.provider.config.name
        old_model = self.provider.config.model

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
        # Keep self.config.provider in sync so resolve_provider() / effective_*
        # helpers (cost calc, token budget, context_window, max_tokens cap)
        # read the NEW provider, not the stale one. (#1)
        self.config.provider = cfg

        if session is not None:
            from agent.core.events import ProviderSwitchEvent

            event = ProviderSwitchEvent(
                from_provider=old_name,
                from_model=old_model,
                to_provider=cfg.name,
                to_model=cfg.model,
            )
            session.append(event)
            self._warn_capability_mismatch(session)

        return f"{cfg.name}/{cfg.model}"

    def _warn_capability_mismatch(self, session: Session) -> None:
        """Log warnings if the new provider lacks capabilities used in the session so far."""
        from agent.core.events import ToolCall, UserMessage

        has_tool_calls = any(isinstance(e, ToolCall) for e in session.events)
        has_images = any(isinstance(e, UserMessage) and e.is_multimodal for e in session.events)

        if has_tool_calls and not self.provider.supports_tools:
            logger.warning(
                "New provider %s/%s does not support tools, but session has tool call history",
                self.provider.config.name,
                self.provider.config.model,
            )
        if has_images and not self.provider.supports_vision:
            logger.warning(
                "New provider %s/%s does not support vision, but session has image content",
                self.provider.config.name,
                self.provider.config.model,
            )

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

        # Rebuild prompt with updated tool set (includes extension tools)
        self._rebuild_system_prompt()

        # Append system prompt additions from extensions
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
                        # #7 — Wrap the coroutine so exceptions are logged
                        # instead of vanishing into the Task's done-state.
                        # Strong-ref the Task so the GC doesn't reap it
                        # mid-flight; the done-callback removes it on completion.
                        task = asyncio.ensure_future(_safe_async_callback(cb, event))
                        self._on_event_tasks.add(task)
                        task.add_done_callback(self._on_event_tasks.discard)
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
