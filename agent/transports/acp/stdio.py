"""ACP stdio transport — SDK-based agent server for Zed and other editors.

Uses the official ``agent-client-protocol`` Python SDK. This is what
``aar acp`` starts by default and what editors connect to via the
``"type": "custom"`` agent-server setting in their config.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from agent.core.agent import Agent as AarAgent
from agent.core.config import AgentConfig, ProviderConfig
from agent.core.events import (
    AssistantMessage,
    ContextWindowEvent,
    Event,
    ProviderMeta,
    ReasoningBlock,
    StreamChunk,
    ToolCall,
    ToolResult,
    UserMessage,
)
from agent.core.session import Session
from agent.core.state import AgentState
from agent.memory.session_store import SessionStore, validate_session_id
from agent.safety.permissions import ApprovalCallback
from agent.tools.registry import ToolRegistry

from .common import (
    _acp_server_to_mcp_config,
    _auto_approve,
    _available_commands,
    _build_config_options,
    _build_mode_state,
    _build_tool_result_content,
    _extract_locations,
    _extract_text,
    _load_default_config,
    _map_stop_reason,
    _model_id_to_provider,
    _side_effects_to_tool_kind,
)

logger = logging.getLogger(__name__)


class AarAcpAgent:
    """Aar agent wrapped as an ACP stdio agent via the official SDK.

    Subclasses ``acp.Agent`` (from the ``agent-client-protocol`` package)
    at runtime inside ``run_acp_stdio``. Zed and other ACP-compatible
    editors communicate with this class over stdin/stdout using the SDK's
    framing — no HTTP server needed.

    The SDK is imported lazily so the package is optional at import time.
    """

    # Delay (seconds) before optimistic AvailableCommandsUpdate push.
    # Gives the client time to register the session before the notification
    # arrives.  Override to 0 in tests.
    _PUSH_COMMANDS_DELAY: float = 0.5

    def __init__(
        self,
        config: AgentConfig | None = None,
        approval_callback: ApprovalCallback | None = None,
        registry: ToolRegistry | None = None,
        agent_name: str = "aar",
    ) -> None:
        self._config = config or _load_default_config()
        self._default_approval: ApprovalCallback = approval_callback or _auto_approve
        self._registry = registry
        self._agent_name = agent_name
        self._store = SessionStore(self._config.session_dir)
        self._sessions: dict[str, Session] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        # Running prompt tasks — stored so cancel() can call task.cancel()
        self._run_tasks: dict[str, asyncio.Task] = {}
        # Per-session MCP state (started MCPBridge + its ToolRegistry)
        self._mcp_bridges: dict[str, Any] = {}
        self._session_registries: dict[str, ToolRegistry] = {}
        # Per-session model override (from set_session_model)
        self._session_configs: dict[str, AgentConfig] = {}
        # Sessions that have already received AvailableCommandsUpdate
        self._commands_pushed: set[str] = set()
        # Per-session current mode id (from set_session_mode)
        self._session_modes: dict[str, str] = {}
        # Capabilities the peer advertised in ``initialize``. Populated by
        # ``initialize()``; used to gate features we only expose when the
        # client supports them (e.g. the ``acp_terminal`` tool).
        self._client_capabilities: Any = None
        self._conn: Any = None  # acp.interfaces.Client, set in on_connect
        # Per-session async locks — serialize lifecycle mutations
        # (new/load/close/prompt-setup) so two ACP requests for the same
        # session don't corrupt the per-session dicts above.
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Strong refs for fire-and-forget tasks (push_available_commands,
        # session_update, etc). Without this set the tasks can be GC'd
        # mid-flight; the done-callback also surfaces silent exceptions.
        self._background_tasks: set[asyncio.Task] = set()
        # Per-session extension managers — loaded at session creation so that
        # extension slash-commands are available before the agent loop runs.
        self._extension_managers: dict[str, Any] = {}

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    def _client_supports_terminal(self) -> bool:
        """Return True when the connected client advertised ``terminal`` support.

        Defaults to False when ``initialize`` has not run yet or the client
        sent no capabilities block, so we never issue ``terminal/create`` to
        a peer that would reject it as method-not-found.
        """
        return bool(getattr(self._client_capabilities, "terminal", False))

    def _spawn(self, coro: Coroutine[Any, Any, Any], *, name: str = "") -> asyncio.Task:
        """Create a tracked fire-and-forget task.

        The task is held in ``_background_tasks`` until it completes, so it
        cannot be garbage-collected mid-flight. Exceptions that the caller
        never awaits are surfaced to the logger instead of being silently
        dropped by asyncio.
        """
        task = asyncio.create_task(coro, name=name or "acp-background")
        self._background_tasks.add(task)
        task.add_done_callback(self._on_background_done)
        return task

    def _on_background_done(self, task: asyncio.Task) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("ACP: background task %r failed: %s", task.get_name(), exc)

    async def shutdown(self) -> None:
        """Wait for any in-flight background tasks to finish.

        Call from the server stop path to avoid warnings about pending tasks
        and to give session_update notifications a chance to flush.
        """
        if not self._background_tasks:
            return
        pending = list(self._background_tasks)
        await asyncio.gather(*pending, return_exceptions=True)

    # ------------------------------------------------------------------
    # ACP SDK lifecycle hooks
    # ------------------------------------------------------------------

    def on_connect(self, conn: Any) -> None:
        self._conn = conn

    async def _push_available_commands(
        self,
        session_id: str,
        *,
        delay: float = 0.0,
    ) -> None:
        """Send ``AvailableCommandsUpdate`` to the client for *session_id*.

        *delay* — seconds to ``asyncio.sleep`` before sending.  The early
        push from ``new_session`` / ``load_session`` / ``fork_session`` uses
        a short delay so that the response that creates the session reaches
        the client before this notification.  Without the delay, the client
        may drop the notification because it hasn't registered the session
        yet (Zed logs ``Received session notification for unknown session``).

        Does **not** check or mutate ``_commands_pushed`` — callers are
        responsible for gating and recording delivery.
        """
        from acp.schema import AvailableCommandsUpdate

        if delay > 0:
            await asyncio.sleep(delay)
        if self._conn:
            ext_mgr = self._extension_managers.get(session_id)
            ext_extra = (
                {name: desc for name, (desc, _) in ext_mgr.commands.items()} if ext_mgr else None
            )
            await self._conn.session_update(
                session_id=session_id,
                update=AvailableCommandsUpdate(
                    available_commands=_available_commands(ext_extra),
                    session_update="available_commands_update",
                ),
                source=self._agent_name,
            )

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any = None,
        client_info: Any = None,
        **kwargs: Any,
    ) -> Any:
        from acp import PROTOCOL_VERSION, InitializeResponse
        from acp.schema import (
            AgentCapabilities,
            Implementation,
            McpCapabilities,
            PromptCapabilities,
            SessionCapabilities,
            SessionCloseCapabilities,
            SessionForkCapabilities,
            SessionListCapabilities,
            SessionResumeCapabilities,
        )

        try:
            from acp.schema import SessionAdditionalDirectoriesCapabilities
        except ImportError:  # SDK < 0.12.2
            SessionAdditionalDirectoriesCapabilities = None

        self._client_capabilities = client_capabilities

        session_caps_kwargs: dict[str, Any] = dict(
            list=SessionListCapabilities(),
            close=SessionCloseCapabilities(),
            fork=SessionForkCapabilities(),
            resume=SessionResumeCapabilities(),
        )
        if SessionAdditionalDirectoriesCapabilities is not None:
            session_caps_kwargs["additional_directories"] = (
                SessionAdditionalDirectoriesCapabilities()
            )

        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(
                load_session=True,
                mcp_capabilities=McpCapabilities(http=True, sse=False),
                prompt_capabilities=PromptCapabilities(embedded_context=True),
                session_capabilities=SessionCapabilities(**session_caps_kwargs),
            ),
            agent_info=Implementation(name="aar", title="Aar Agent", version="0.4.0"),
        )

    async def new_session(
        self,
        cwd: str = "",
        mcp_servers: list | None = None,
        additional_directories: list | None = None,
        **kwargs: Any,
    ) -> Any:
        from acp import NewSessionResponse

        session = Session(metadata={"cwd": cwd} if cwd else {})
        session.metadata["additional_directories"] = additional_directories or []
        sid = session.session_id
        async with self._session_lock(sid):
            self._sessions[sid] = session
            self._store.save(session)
            await self._setup_mcp(sid, mcp_servers or [])
            await self._setup_extensions(sid, session)
        # Fire-and-forget: push commands early with a short delay so the
        # session/new response reaches the client first.  Without the delay
        # Zed drops the notification ("unknown session").  prompt() still
        # acts as the guaranteed delivery fallback.
        self._spawn(
            self._push_available_commands(sid, delay=self._PUSH_COMMANDS_DELAY),
            name=f"push-cmds-{sid}",
        )
        logger.info("ACP: new session %s cwd=%r", sid, cwd)
        cfg = self._session_configs.get(sid, self._config)
        resp = NewSessionResponse(
            session_id=sid,
            modes=_build_mode_state(cfg.safety, self._session_modes.get(sid)),
            config_options=_build_config_options(
                cfg.safety,
                cfg.resolve_provider(),
                self._session_modes.get(sid),
                providers=cfg.providers,
            ),
        )
        logger.debug(
            "ACP: new_session response payload: %s",
            resp.model_dump_json(by_alias=True, exclude_none=True),
        )
        return resp

    async def load_session(
        self,
        cwd: str = "",
        session_id: str = "",
        mcp_servers: list | None = None,
        additional_directories: list | None = None,
        **kwargs: Any,
    ) -> Any:
        """Resume a previously saved session.

        Per the ACP spec the Agent MUST replay the full conversation history to
        the Client via ``session/update`` notifications (``UserMessageChunk`` and
        ``AgentMessageChunk``) before responding to ``session/load``.

        Returns ``None`` when the session is not found so the Client can fall
        back to creating a new one.
        """
        from acp import LoadSessionResponse
        from acp.schema import AgentMessageChunk, TextContentBlock, UserMessageChunk

        try:
            validate_session_id(session_id)
        except ValueError as exc:
            logger.info("ACP: session %s not found (%s)", session_id, exc)
            return None
        try:
            async with self._session_lock(session_id):
                session = self._store.load(session_id)
                if cwd:
                    session.metadata["cwd"] = cwd
                session.metadata["additional_directories"] = additional_directories or []
                self._sessions[session_id] = session
                await self._setup_mcp(session_id, mcp_servers or [])
                await self._setup_extensions(session_id, session)

            # Replay conversation history per ACP spec.
            # The Agent MUST replay the entire conversation via session/update
            # notifications before responding to session/load.
            if self._conn:
                for event in session.events:
                    if isinstance(event, UserMessage) and event.content:
                        await self._conn.session_update(
                            session_id=session_id,
                            update=UserMessageChunk(
                                session_update="user_message_chunk",
                                content=TextContentBlock(type="text", text=event.content),
                            ),
                            source=self._agent_name,
                        )
                    elif isinstance(event, AssistantMessage) and event.content:
                        await self._conn.session_update(
                            session_id=session_id,
                            update=AgentMessageChunk(
                                session_update="agent_message_chunk",
                                content=TextContentBlock(type="text", text=event.content),
                            ),
                            source=self._agent_name,
                        )

            self._spawn(
                self._push_available_commands(session_id, delay=self._PUSH_COMMANDS_DELAY),
                name=f"push-cmds-{session_id}",
            )
            logger.info("ACP: loaded session %s (%d events)", session_id, len(session.events))
            cfg = self._session_configs.get(session_id, self._config)
            resp = LoadSessionResponse(
                modes=_build_mode_state(cfg.safety, self._session_modes.get(session_id)),
                config_options=_build_config_options(
                    cfg.safety,
                    cfg.resolve_provider(),
                    self._session_modes.get(session_id),
                    providers=cfg.providers,
                ),
            )
            logger.debug(
                "ACP: load_session response payload: %s",
                resp.model_dump_json(by_alias=True, exclude_none=True),
            )
            return resp
        except (FileNotFoundError, ValueError) as exc:
            logger.info("ACP: session %s not found (%s)", session_id, exc)
            return None

    async def list_sessions(
        self,
        cursor: str | None = None,
        cwd: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Return all persisted sessions so Zed can show session history."""
        import datetime

        from acp.schema import ListSessionsResponse, SessionInfo

        session_infos: list[SessionInfo] = []
        for sid in self._store.list_sessions():
            try:
                s = self._store.load(sid)
                title = next(
                    (
                        e.content[:60]
                        for e in s.events
                        if isinstance(e, AssistantMessage) and e.content
                    ),
                    sid[:12],
                )
                session_cwd = s.metadata.get("cwd", "") if s.metadata else ""
                last_ts = s.events[-1].timestamp if s.events else None
                updated_at = (
                    datetime.datetime.fromtimestamp(last_ts, tz=datetime.timezone.utc).isoformat()
                    if last_ts is not None
                    else None
                )
                session_infos.append(
                    SessionInfo(
                        session_id=sid,
                        cwd=session_cwd,
                        title=title,
                        updated_at=updated_at,
                    )
                )
            except Exception:
                session_infos.append(SessionInfo(session_id=sid, cwd="", title=sid[:12]))
        # Filter by cwd when requested (spec: "Only sessions with a matching cwd are returned")
        if cwd:
            session_infos = [s for s in session_infos if s.cwd == cwd]

        # Newest-first — sessions with no timestamp sort to the end
        session_infos.sort(key=lambda s: s.updated_at or "", reverse=True)

        return ListSessionsResponse(sessions=session_infos)

    async def close_session(self, session_id: str, **kwargs: Any) -> Any:
        """Clean up per-session state when Zed closes a session."""
        from acp.schema import CloseSessionResponse

        try:
            validate_session_id(session_id)
        except ValueError:
            logger.warning("ACP: close_session ignoring invalid session_id %r", session_id)
            return CloseSessionResponse()

        # Cancel any prompt still running for this session and await its
        # cleanup. Done outside the session lock — prompt() releases the
        # lock while awaiting run_task, so cancellation can proceed without
        # deadlocking.
        task = self._run_tasks.get(session_id)
        event = self._cancel_events.get(session_id)
        if event is not None:
            event.set()
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — cleanup path
                pass

        async with self._session_lock(session_id):
            await self._teardown_mcp(session_id)
            self._sessions.pop(session_id, None)
            self._cancel_events.pop(session_id, None)
            self._run_tasks.pop(session_id, None)
            self._session_configs.pop(session_id, None)
            self._session_modes.pop(session_id, None)
            self._commands_pushed.discard(session_id)
            self._extension_managers.pop(session_id, None)
        self._session_locks.pop(session_id, None)
        logger.info("ACP: closed session %s", session_id)
        return CloseSessionResponse()

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        """Cancel an ongoing prompt turn.

        Sets the cooperative cancel event AND cancels the running asyncio task
        so the agent stops as soon as possible — even if it's mid-LLM call or
        mid-tool execution. Per the ACP spec the ``session/prompt`` response
        MUST use ``stop_reason="cancelled"``.
        """
        try:
            validate_session_id(session_id)
        except ValueError:
            logger.warning("ACP: cancel ignoring invalid session_id %r", session_id)
            return
        event = self._cancel_events.get(session_id)
        if event:
            event.set()

        task = self._run_tasks.get(session_id)
        if task and not task.done():
            task.cancel()

        if event or task:
            logger.info("ACP: cancel requested for session %s", session_id)
        else:
            logger.debug("ACP: cancel for session %s — no active prompt", session_id)

    async def set_session_model(
        self,
        model_id: str,
        session_id: str,
        **kwargs: Any,
    ) -> Any:
        """Switch the model for an existing session (unstable protocol).

        ``session/set_model`` was an *unstable* ACP method and the SDK dropped
        it in 0.12 (``SetSessionModelResponse`` no longer exists, and
        ``acp.Agent`` no longer declares the method). We keep the behaviour for
        older SDKs — and for direct programmatic callers — and simply return
        ``None`` when the response type is gone, since no 0.12+ client can
        route to it anyway.
        """
        try:
            from acp.schema import SetSessionModelResponse  # type: ignore[attr-defined]
        except ImportError:  # acp >= 0.12 removed the unstable set_model API
            SetSessionModelResponse = None  # type: ignore[assignment]

        validate_session_id(session_id)
        base_cfg = self._session_configs.get(session_id, self._config)

        # Check if model_id is a named provider key in the config registry
        if model_id in base_cfg.providers:
            new_provider = base_cfg.providers[model_id]
        else:
            provider_name, model = _model_id_to_provider(model_id)
            new_provider = (
                base_cfg.provider
                if isinstance(base_cfg.provider, ProviderConfig)
                else base_cfg.resolve_provider()
            )
            new_provider = new_provider.model_copy(
                update={"name": provider_name, "model": model},
            )

        self._session_configs[session_id] = base_cfg.model_copy(
            update={"provider": new_provider},
        )
        logger.info(
            "ACP: session %s model → %s/%s",
            session_id,
            new_provider.name,
            new_provider.model,
        )
        return SetSessionModelResponse() if SetSessionModelResponse is not None else None

    async def authenticate(self, method_id: str, **kwargs: Any) -> Any:
        """Handle ``authenticate`` — Aar has no auth methods, so this is a no-op.

        Providers are configured via env vars (ANTHROPIC_API_KEY, OPENAI_API_KEY, …)
        loaded by ``_load_default_config``. We still return a valid
        ``AuthenticateResponse`` so clients that probe the method succeed.
        """
        from acp.schema import AuthenticateResponse

        logger.info("ACP: authenticate requested (method_id=%r) — no-op", method_id)
        return AuthenticateResponse()

    async def set_session_mode(
        self,
        mode_id: str,
        session_id: str,
        **kwargs: Any,
    ) -> Any:
        """Switch the Agent's mode for *session_id*.

        Aar exposes three modes that map onto the existing ``SafetyConfig``:

        * ``auto``       — no approval required (writes + execute auto-allowed)
        * ``review``     — approvals required for writes and execute (default)
        * ``read-only``  — only read-only operations; writes/execute disabled

        Pushes both a ``CurrentModeUpdate`` (legacy) and a ``ConfigOptionUpdate``
        (current spec) so all client versions track the change.
        """
        from acp.schema import ConfigOptionUpdate, CurrentModeUpdate, SetSessionModeResponse

        validate_session_id(session_id)
        base_cfg = self._session_configs.get(session_id, self._config)
        safety = base_cfg.safety

        if mode_id == "auto":
            new_safety = safety.model_copy(
                update={
                    "require_approval_for_writes": False,
                    "require_approval_for_execute": False,
                    "read_only": False,
                }
            )
        elif mode_id == "review":
            new_safety = safety.model_copy(
                update={
                    "require_approval_for_writes": True,
                    "require_approval_for_execute": True,
                    "read_only": False,
                }
            )
        elif mode_id == "read-only":
            new_safety = safety.model_copy(
                update={
                    "require_approval_for_writes": True,
                    "require_approval_for_execute": True,
                    "read_only": True,
                }
            )
        else:
            raise ValueError(f"Unknown session mode: {mode_id!r}")

        self._session_configs[session_id] = base_cfg.model_copy(update={"safety": new_safety})
        self._session_modes[session_id] = mode_id

        if self._conn is not None:
            # Legacy mode update — for clients that use the older `modes` API.
            self._spawn(
                self._conn.session_update(
                    session_id=session_id,
                    update=CurrentModeUpdate(
                        session_update="current_mode_update",
                        current_mode_id=mode_id,
                    ),
                    source=self._agent_name,
                ),
                name=f"mode-update-{session_id}",
            )
            # Current spec: push full configOptions so the model/mode/config
            # dropdowns stay in sync for clients that use configOptions.
            cfg = self._session_configs.get(session_id, self._config)
            self._spawn(
                self._conn.session_update(
                    session_id=session_id,
                    update=ConfigOptionUpdate(
                        session_update="config_option_update",
                        config_options=_build_config_options(
                            cfg.safety,
                            cfg.resolve_provider(),
                            mode_id,
                            providers=cfg.providers,
                        ),
                    ),
                    source=self._agent_name,
                ),
                name=f"config-update-{session_id}",
            )

        logger.info("ACP: session %s mode → %s", session_id, mode_id)
        return SetSessionModeResponse()

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: Any,
        **kwargs: Any,
    ) -> Any:
        """Update a per-session configuration option.

        Supported options:

        * ``model`` (select) — switch the active model
        * ``mode``  (select) — ``auto`` / ``review`` / ``read-only``

        Always returns the **complete** updated ``configOptions`` list as
        required by the ACP spec.
        """
        from acp.schema import SetSessionConfigOptionResponse

        validate_session_id(session_id)
        base_cfg = self._session_configs.get(session_id, self._config)
        safety = base_cfg.safety
        provider = base_cfg.resolve_provider()

        if config_id == "model":
            model_id = str(value)
            # Check if model_id is a named provider key in the config registry
            if model_id in base_cfg.providers:
                new_provider = base_cfg.providers[model_id]
            else:
                p_name, p_model = _model_id_to_provider(model_id)
                new_provider = provider.model_copy(
                    update={"name": p_name, "model": p_model},
                )
            self._session_configs[session_id] = base_cfg.model_copy(
                update={"provider": new_provider}
            )
            provider = new_provider
            logger.info(
                "ACP: session %s model → %s/%s (via set_config_option)",
                session_id,
                new_provider.name,
                new_provider.model,
            )
        elif config_id == "mode":
            mode_id = str(value)
            if mode_id == "auto":
                new_safety = safety.model_copy(
                    update={
                        "require_approval_for_writes": False,
                        "require_approval_for_execute": False,
                        "read_only": False,
                    }
                )
            elif mode_id == "review":
                new_safety = safety.model_copy(
                    update={
                        "require_approval_for_writes": True,
                        "require_approval_for_execute": True,
                        "read_only": False,
                    }
                )
            elif mode_id == "read-only":
                new_safety = safety.model_copy(
                    update={
                        "require_approval_for_writes": True,
                        "require_approval_for_execute": True,
                        "read_only": True,
                    }
                )
            else:
                raise ValueError(f"Unknown mode: {mode_id!r}")
            self._session_configs[session_id] = base_cfg.model_copy(update={"safety": new_safety})
            self._session_modes[session_id] = mode_id
            safety = new_safety
            logger.info("ACP: session %s mode → %s (via set_config_option)", session_id, mode_id)
        else:
            raise ValueError(f"Unknown config option: {config_id!r}")

        config_opts = _build_config_options(
            safety,
            provider,
            self._session_modes.get(session_id),
            providers=base_cfg.providers,
        )
        return SetSessionConfigOptionResponse(config_options=config_opts)

    async def fork_session(
        self,
        cwd: str = "",
        session_id: str = "",
        mcp_servers: list | None = None,
        additional_directories: list | None = None,
        **kwargs: Any,
    ) -> Any:
        """Create a new session that starts with a deep copy of *session_id*'s events.

        Per the ACP spec, forking lets the client branch a conversation —
        the new session has an independent ID and event stream but carries
        over the full history. MCP servers are re-negotiated for the new
        session id.
        """
        from acp.schema import ForkSessionResponse

        validate_session_id(session_id)

        source = self._sessions.get(session_id)
        if source is None:
            try:
                source = self._store.load(session_id)
            except (FileNotFoundError, ValueError) as exc:
                raise ValueError(f"Cannot fork: session {session_id!r} not found") from exc

        forked = Session(
            events=[e.model_copy(deep=True) for e in source.events],
            metadata={**source.metadata, "forked_from": session_id},
        )
        if cwd:
            forked.metadata["cwd"] = cwd
        forked.metadata["additional_directories"] = additional_directories or []

        new_sid = forked.session_id
        async with self._session_lock(new_sid):
            self._sessions[new_sid] = forked
            # Carry over per-session config (model, safety) so the fork has
            # the same settings as the source.
            if session_id in self._session_configs:
                self._session_configs[new_sid] = self._session_configs[session_id]
            if session_id in self._session_modes:
                self._session_modes[new_sid] = self._session_modes[session_id]
            self._store.save(forked)
            await self._setup_mcp(new_sid, mcp_servers or [])
            await self._setup_extensions(new_sid, forked)

        self._spawn(
            self._push_available_commands(new_sid, delay=self._PUSH_COMMANDS_DELAY),
            name=f"push-cmds-{new_sid}",
        )
        logger.info(
            "ACP: forked session %s → %s (%d events)", session_id, new_sid, len(forked.events)
        )
        return ForkSessionResponse(session_id=new_sid)

    async def resume_session(
        self,
        cwd: str = "",
        session_id: str = "",
        mcp_servers: list | None = None,
        additional_directories: list | None = None,
        **kwargs: Any,
    ) -> Any:
        """Resume a previously saved session WITHOUT replaying history.

        Unlike ``session/load``, ``session/resume`` does not re-send every
        event to the client — the client already knows the history and only
        needs the agent to reattach state (per-session registries, model,
        safety, MCP bridges) so follow-up prompts operate on the same
        session.
        """
        from acp.schema import ResumeSessionResponse

        validate_session_id(session_id)

        try:
            async with self._session_lock(session_id):
                session = self._store.load(session_id)
                if cwd:
                    session.metadata["cwd"] = cwd
                session.metadata["additional_directories"] = additional_directories or []
                self._sessions[session_id] = session
                await self._setup_mcp(session_id, mcp_servers or [])
                await self._setup_extensions(session_id, session)
            self._spawn(
                self._push_available_commands(session_id, delay=self._PUSH_COMMANDS_DELAY),
                name=f"push-cmds-{session_id}",
            )
            logger.info("ACP: resumed session %s (%d events)", session_id, len(session.events))
            return ResumeSessionResponse()
        except (FileNotFoundError, ValueError) as exc:
            raise ValueError(f"Cannot resume: session {session_id!r} not found") from exc

    async def prompt(
        self,
        prompt: list,
        session_id: str,
        **kwargs: Any,
    ) -> Any:
        from acp import PromptResponse, text_block, update_agent_message
        from acp.schema import (
            AgentThoughtChunk,
            Cost,
            SessionInfoUpdate,
            TextContentBlock,
            ToolCallLocation,
            ToolCallProgress,
            ToolCallStart,
            UsageUpdate,
        )

        validate_session_id(session_id)
        text = _extract_text(prompt)

        message_id: str | None = kwargs.get("message_id")
        _resp_extra: dict[str, Any] = {}
        if message_id is not None:
            _resp_extra["user_message_id"] = message_id

        # Reject concurrent prompts for the same session. Per the ACP spec
        # only one prompt turn may be in flight per session at a time. A
        # misbehaving client that sends a second prompt would otherwise
        # silently overwrite _cancel_events[sid] and _run_tasks[sid].
        existing = self._run_tasks.get(session_id)
        if existing is not None and not existing.done():
            raise RuntimeError(
                f"Prompt already in flight for session {session_id}; "
                "cancel the previous prompt before starting a new one"
            )

        # Entry-phase setup is guarded by the per-session lock so cancel()
        # or a second prompt() call cannot observe half-populated dicts.
        # The long-running body (await run_task) runs outside the lock.
        async with self._session_lock(session_id):
            session = self._sessions.get(session_id)
            if session is None:
                try:
                    session = self._store.load(session_id)
                    self._sessions[session_id] = session
                except (FileNotFoundError, ValueError):
                    logger.info("ACP: creating fresh session for id %s", session_id)
                    session = Session(session_id=session_id)
                    self._sessions[session_id] = session

            # Lazily initialize extensions if session was created without new_session().
            if session_id not in self._extension_managers:
                await self._setup_extensions(session_id, session)

            cancel_event = asyncio.Event()
            self._cancel_events[session_id] = cancel_event

            first_push = session_id not in self._commands_pushed
            if first_push:
                self._commands_pushed.add(session_id)

        update_tasks: list[asyncio.Task] = []
        streamed_chunks = False
        title_sent = False
        _ctx_window_size: int = (
            0  # updated by ContextWindowEvent; used to fix UsageUpdate semantics
        )

        if first_push:
            _push_now = self._spawn(
                self._push_available_commands(session_id),
                name=f"push-cmds-{session_id}",
            )
            update_tasks.append(_push_now)

        def _push(update: Any) -> None:
            if self._conn:
                task = asyncio.create_task(
                    self._conn.session_update(
                        session_id=session_id,
                        update=update,
                        source=self._agent_name,
                    )
                )
                update_tasks.append(task)

        _tc_args: dict[str, dict[str, Any]] = {}

        def on_event(event: Event) -> None:
            nonlocal streamed_chunks, title_sent, _ctx_window_size

            if isinstance(event, StreamChunk) and not event.finished and event.text:
                streamed_chunks = True
                _push(update_agent_message(text_block(event.text)))

            elif isinstance(event, AssistantMessage) and event.content:
                if not streamed_chunks:
                    _push(update_agent_message(text_block(event.content)))
                if not title_sent:
                    title_sent = True
                    _push(
                        SessionInfoUpdate(
                            title=text[:60] if text else event.content[:60],
                            session_update="session_info_update",
                        )
                    )

            elif isinstance(event, ReasoningBlock) and event.content:
                _push(
                    AgentThoughtChunk(
                        content=TextContentBlock(type="text", text=event.content),
                        session_update="agent_thought_chunk",
                    )
                )

            elif isinstance(event, ToolCall):
                # tool_call_id is guaranteed at construction (ToolCall model
                # validator fills it with a uuid4 when providers leave it empty).
                tc_id = event.tool_call_id
                registry = self._session_registries.get(session_id, self._registry)
                _spec = registry.get(event.tool_name) if registry else None
                _kind = _side_effects_to_tool_kind(
                    _spec.side_effects if _spec else [], event.tool_name
                )
                _loc_paths = _extract_locations(event.arguments)
                _locations = [ToolCallLocation(path=p) for p in _loc_paths] if _loc_paths else None
                # Status MUST be "pending" — the tool hasn't started yet and
                # may be awaiting approval. Zed only shows permission buttons
                # for tool calls that are still in "pending" status.
                _push(
                    ToolCallStart(
                        title=event.tool_name,
                        tool_call_id=tc_id,
                        kind=_kind,
                        status="pending",
                        raw_input=event.arguments,
                        locations=_locations,
                        session_update="tool_call",
                    )
                )
                _tc_args[tc_id] = event.arguments

            elif isinstance(event, ToolResult):
                tc_id = event.tool_call_id
                # Emit in_progress before the terminal status so clients see
                # the full pending → in_progress → completed/failed lifecycle.
                _push(
                    ToolCallProgress(
                        title=event.tool_name,
                        tool_call_id=tc_id,
                        status="in_progress",
                        session_update="tool_call_update",
                    )
                )
                _content, _raw_output = _build_tool_result_content(
                    event.tool_name,
                    _tc_args.get(tc_id, {}),
                    event.output,
                    event.is_error,
                )
                _push(
                    ToolCallProgress(
                        title=event.tool_name,
                        tool_call_id=tc_id,
                        status="failed" if event.is_error else "completed",
                        content=_content,
                        raw_output=_raw_output,
                        session_update="tool_call_update",
                    )
                )

            elif isinstance(event, ContextWindowEvent):
                # Track the context window capacity so the UsageUpdate below
                # can send the correct ACP semantics: size=capacity, used=filled.
                _ctx_window_size = event.ctx_window

            elif isinstance(event, ProviderMeta) and event.usage:
                # ACP UsageUpdate semantics (from the SDK schema):
                #   size  — total context window capacity in tokens
                #   used  — tokens currently in context (input tokens this turn)
                # When sliding-window management is active we have accurate
                # values from ContextWindowEvent; otherwise fall back to
                # input_tokens as the best available approximation.
                _input = event.usage.get("input_tokens", 0)
                size = _ctx_window_size if _ctx_window_size > 0 else _input
                used = _input
                _push(
                    UsageUpdate(
                        cost=Cost(
                            amount=round(getattr(session, "total_cost", 0.0), 6), currency="usd"
                        ),
                        size=size,
                        used=used,
                        session_update="usage_update",
                    )
                )

        # Handle slash commands locally — no agent loop needed
        cmd = text.strip().split()[0].lower() if text.strip().startswith("/") else ""
        if cmd in ("/status", "/tools", "/policy", "/model"):
            cmd_args = (
                text.strip().split(maxsplit=1)[1] if len(text.strip().split(maxsplit=1)) > 1 else ""
            )
            reply = self._handle_slash_command(cmd, session_id, session, args=cmd_args)
            _push(update_agent_message(text_block(reply)))
            if update_tasks:
                await asyncio.gather(*update_tasks, return_exceptions=True)
            self._cancel_events.pop(session_id, None)
            return PromptResponse(stop_reason="end_turn", **_resp_extra)

        # Handle extension slash commands
        if cmd:
            ext_mgr = self._extension_managers.get(session_id)
            if ext_mgr is not None:
                cmd_name = cmd[1:]  # strip leading "/"
                ext_cmds = ext_mgr.commands
                if cmd_name in ext_cmds:
                    ext_mgr.update_session(session)
                    args_str = text.strip()[len(cmd) :].strip()
                    _, handler = ext_cmds[cmd_name]
                    try:
                        result = handler(args_str, ext_mgr._context)
                        if asyncio.iscoroutine(result):
                            result = await result
                        reply = str(result) if result is not None else ""
                    except Exception as exc:
                        logger.error("ACP: extension command %r error: %s", cmd_name, exc)
                        reply = f"Extension command error: {exc}"
                    _push(update_agent_message(text_block(reply)))
                    if update_tasks:
                        await asyncio.gather(*update_tasks, return_exceptions=True)
                    self._cancel_events.pop(session_id, None)
                    self._push_panel_changes(session_id)
                    return PromptResponse(stop_reason="end_turn", **_resp_extra)

        # Build the approval callback: use ACP request_permission when a client
        # is connected (Zed / stdio mode), fall back to the configured default.
        if self._conn is not None:
            from agent.transports.acp_permissions import make_acp_approval_callback

            acp_t = self._config.safety.acp_approval_timeout
            loop_t = self._config.timeout
            if loop_t > 0.0 and (acp_t == 0.0 or acp_t > loop_t):
                logger.warning(
                    "acp_approval_timeout (%s) exceeds the agent loop timeout (%.1fs) — "
                    "approval requests may be cut off before the user responds; set "
                    "acp_approval_timeout <= timeout, or set timeout=0.0 for no loop limit",
                    "indefinite" if acp_t == 0.0 else f"{acp_t:.1f}s",
                    loop_t,
                )

            _approval_cb = make_acp_approval_callback(self._conn, session_id, timeout=acp_t)
        else:
            _approval_cb = self._default_approval

        aar_agent = self._make_aar_agent(session_id=session_id, approval_callback=_approval_cb)
        aar_agent.on_event(on_event)

        run_task = asyncio.create_task(aar_agent.run(text, session, cancel_event=cancel_event))
        self._run_tasks[session_id] = run_task

        try:
            finished = await run_task
        except asyncio.CancelledError:
            # Per ACP spec: catch cancellation and return the semantically
            # meaningful "cancelled" stop reason so clients can reliably
            # confirm the cancellation.
            logger.info("ACP: prompt task cancelled for session %s", session_id)
            finished = session
            finished.state = AgentState.CANCELLED
        finally:
            self._run_tasks.pop(session_id, None)

        if update_tasks:
            await asyncio.gather(*update_tasks, return_exceptions=True)

        self._cancel_events.pop(session_id, None)
        self._sessions[session_id] = finished
        self._store.save(finished)
        self._push_panel_changes(session_id)

        return PromptResponse(stop_reason=_map_stop_reason(finished.state), **_resp_extra)

    # ------------------------------------------------------------------
    # Extension UI panels — custom ``_aar/panel_*`` methods
    # ------------------------------------------------------------------
    #
    # The SDK routes any request whose method starts with ``_`` to
    # ``ext_method(name, params)`` with the underscore stripped, so a client
    # calls ``_aar/panel_list`` and we see ``aar/panel_list``.  Notifications
    # go the other way through ``conn.ext_notification`` (``_aar/panel_changed``).
    # Same data the fixed TUI draws; see docs/acp.md.

    _PANEL_METHODS = ("aar/panel_list", "aar/panel_snapshot", "aar/panel_action")

    @staticmethod
    def _param(params: dict[str, Any], *names: str, default: Any = None) -> Any:
        """First present key among *names* (camelCase on the wire, snake in tests)."""
        for name in names:
            value = params.get(name)
            if value is not None:
                return value
        return default

    def _panel_context(self, params: dict[str, Any]) -> tuple[str, Any]:
        """Resolve ``(session_id, ExtensionManager)`` for a panel request."""
        from acp.exceptions import RequestError

        session_id = self._param(params, "sessionId", "session_id")
        if not isinstance(session_id, str) or not session_id:
            raise RequestError.invalid_params({"sessionId": "required"})
        ext_mgr = self._extension_managers.get(session_id)
        if ext_mgr is None:
            raise RequestError.invalid_params({"sessionId": f"unknown session {session_id!r}"})
        session = self._sessions.get(session_id)
        if session is not None:
            ext_mgr.update_session(session)
        return session_id, ext_mgr

    def _resolve_panel(self, ext_mgr: Any, params: dict[str, Any]) -> Any:
        from acp.exceptions import RequestError

        name = self._param(params, "panel")
        panels = ext_mgr.panels
        if not isinstance(name, str) or name not in panels:
            raise RequestError.invalid_params(
                {"panel": f"unknown panel {name!r}", "available": sorted(panels)}
            )
        return panels[name]

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Handle custom ``_aar/*`` requests (see :attr:`_PANEL_METHODS`)."""
        from acp.exceptions import RequestError
        from agent.extensions.api import UIInvocation, run_ui_action, run_ui_snapshot

        if method not in self._PANEL_METHODS:
            raise RequestError.method_not_found(f"_{method}")
        params = params if isinstance(params, dict) else {}
        session_id, ext_mgr = self._panel_context(params)
        ctx = ext_mgr._context

        if method == "aar/panel_list":
            return {
                "panels": [
                    {
                        "name": p.name,
                        "title": p.title,
                        "status": p.status_text(ctx),
                        "actions": [a.to_dict() for a in p.actions],
                    }
                    for p in ext_mgr.panels.values()
                ]
            }

        panel = self._resolve_panel(ext_mgr, params)

        if method == "aar/panel_snapshot":
            root = await run_ui_snapshot(panel, ctx)
            panel.changed.clear()
            return {"panel": panel.name, "root": root.to_dict(), "status": panel.status_text(ctx)}

        # aar/panel_action
        action_id = self._param(params, "action")
        action = panel.action(action_id) if isinstance(action_id, str) else None
        if action is None:
            raise RequestError.invalid_params(
                {
                    "action": f"unknown action {action_id!r}",
                    "available": [a.id for a in panel.actions],
                }
            )
        node_id = self._param(params, "nodeId", "node_id")
        root = await run_ui_snapshot(panel, ctx)
        node = root.find(node_id) if isinstance(node_id, str) else None
        if node is None:
            raise RequestError.invalid_params({"nodeId": f"unknown node {node_id!r}"})
        if not action.applies_to(node):
            raise RequestError.invalid_params(
                {"action": f"{action.id!r} does not apply to node kind {node.kind!r}"}
            )
        running = self._run_tasks.get(session_id)
        if action.mutates and running is not None and not running.done():
            raise RequestError.invalid_params(
                {"action": "a prompt is in flight for this session — cancel it first"}
            )
        args = self._param(params, "args", default={})
        args = dict(args) if isinstance(args, dict) else {}
        try:
            message = await run_ui_action(action, UIInvocation(node=node, ctx=ctx, args=args))
        except Exception as exc:
            logger.error("ACP: panel action %s/%s failed: %s", panel.name, action.id, exc)
            message = f"✗ {action.id}: {exc}"
        root = await run_ui_snapshot(panel, ctx)
        panel.changed.clear()
        if action.mutates:
            session = self._sessions.get(session_id)
            if session is not None:
                self._store.save(session)
        return {
            "panel": panel.name,
            "action": action.id,
            "message": message,
            "root": root.to_dict(),
            "status": panel.status_text(ctx),
        }

    def _push_panel_changes(self, session_id: str) -> None:
        """Notify the client (``_aar/panel_changed``) for every panel whose
        ``changed`` flag is set.  Fire-and-forget; the flag is cleared when the
        client fetches a fresh snapshot."""
        if self._conn is None:
            return
        ext_mgr = self._extension_managers.get(session_id)
        if ext_mgr is None:
            return
        for panel in ext_mgr.panels.values():
            if not panel.changed.is_set():
                continue
            self._spawn(
                self._conn.ext_notification(
                    "aar/panel_changed", {"sessionId": session_id, "panel": panel.name}
                ),
                name=f"panel-changed-{session_id}-{panel.name}",
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_aar_agent(
        self,
        session_id: str = "",
        approval_callback: ApprovalCallback | None = None,
    ) -> AarAgent:
        config = self._session_configs.get(session_id, self._config)
        registry = self._session_registries.get(session_id, self._registry)
        return AarAgent(
            config=config,
            approval_callback=approval_callback or self._default_approval,
            registry=registry,
        )

    def _handle_slash_command(
        self,
        cmd: str,
        session_id: str,
        session: Session,
        args: str = "",
    ) -> str:
        """Return a plain-text reply for a built-in slash command."""
        cfg = self._session_configs.get(session_id, self._config)
        registry = self._session_registries.get(session_id, self._registry)

        if cmd == "/model":
            active = cfg.resolve_provider()
            if not args:
                lines = [
                    f"**Active:** {active.name}/{active.model}",
                ]
                if cfg.providers:
                    lines.append("")
                    lines.append("**Available providers:**")
                    for key, pcfg in cfg.providers.items():
                        marker = (
                            " *(active)*"
                            if (pcfg.name == active.name and pcfg.model == active.model)
                            else ""
                        )
                        lines.append(f"- `{key}` → {pcfg.name}/{pcfg.model}{marker}")
                else:
                    lines.append(
                        "\nNo named providers configured. "
                        "Use `/model <provider/model>` for ad-hoc switch."
                    )
                return "\n".join(lines)
            # Switch
            key = args.strip()
            if key in cfg.providers:
                new_provider = cfg.providers[key]
            elif "/" in key:
                provider_name, model = key.split("/", 1)
                new_provider = active.model_copy(
                    update={"name": provider_name, "model": model},
                )
            else:
                provider_name, model = _model_id_to_provider(key)
                new_provider = active.model_copy(
                    update={"name": provider_name, "model": model},
                )
            self._session_configs[session_id] = cfg.model_copy(
                update={"provider": new_provider},
            )
            logger.info(
                "ACP: session %s model → %s/%s (via /model)",
                session_id,
                new_provider.name,
                new_provider.model,
            )
            return f"Switched to **{new_provider.name}/{new_provider.model}**"

        if cmd == "/status":
            lines = [
                f"**Session:** `{session_id}`",
                f"**Provider:** {cfg.resolve_provider().name}",
                f"**Model:** {cfg.resolve_provider().model}",
                f"**Steps this session:** {session.step_count}",
                f"**Messages:** {len(session.events)}",
            ]
            return "\n".join(lines)

        if cmd == "/tools":
            # Start with whatever the session registry has (MCP tools, or empty).
            tools: list = registry.list_tools() if registry else []
            tool_names = {t.name for t in tools}

            # Always add built-in tools from config — they are registered by
            # AarAgent._register_builtins() at prompt time but not present in
            # the session registry that _setup_mcp creates.
            try:
                from agent.tools.builtin.filesystem import register_filesystem_tools
                from agent.tools.builtin.search import register_search_tools
                from agent.tools.builtin.shell import register_shell_tools
                from agent.tools.registry import ToolRegistry as TR

                tmp_reg = TR()
                enabled = set(cfg.tools.enabled_builtins)
                if enabled & {"read_file", "write_file", "edit_file", "list_directory"}:
                    register_filesystem_tools(tmp_reg)
                if "bash" in enabled:
                    register_shell_tools(
                        tmp_reg,
                        default_timeout=cfg.tools.bash_default_timeout,
                        hard_cap=cfg.tools.command_timeout,
                    )
                if enabled & {"grep", "find_files"}:
                    register_search_tools(tmp_reg)
                for name in list(tmp_reg.names()):
                    if name not in enabled:
                        tmp_reg.unregister(name)
                # Merge — skip any built-in whose name already exists in MCP registry
                for t in tmp_reg.list_tools():
                    if t.name not in tool_names:
                        tools.append(t)
            except Exception:
                pass

            if not tools:
                return "No tools registered."
            lines = ["**Available tools:**", ""]
            for t in sorted(tools, key=lambda x: x.name):
                lines.append(f"- **{t.name}** — {t.description or ''}")
            return "\n".join(lines)

        if cmd == "/policy":
            s = cfg.safety
            lines = ["**Safety policy:**", ""]
            lines.append(f"- **Approve writes:** {s.require_approval_for_writes}")
            lines.append(f"- **Approve execute:** {s.require_approval_for_execute}")
            lines.append(f"- **Read-only mode:** {s.read_only}")
            lines.append(f"- **Sandbox:** {s.sandbox}")
            if s.allowed_paths:
                lines.append(f"- **Allowed paths:** {', '.join(s.allowed_paths)}")
            return "\n".join(lines)

        return f"Unknown command: {cmd}"

    async def _setup_extensions(self, session_id: str, session: Session) -> None:
        """Initialize an ExtensionManager for *session_id* so extension slash-commands work.

        This runs at session creation (new/load/fork/resume) so that by the time the first
        prompt arrives the extension manager is populated and its commands can be dispatched
        and advertised to the client via ``AvailableCommandsUpdate``.
        """
        from agent.extensions.manager import ExtensionManager

        cfg = self._session_configs.get(session_id, self._config)
        mgr = ExtensionManager()
        try:
            await mgr.initialize(session, cfg, cancel_event=None)
            logger.info(
                "ACP: loaded %d extension(s) (%d command(s)) for session %s",
                len(mgr.loaded_extensions),
                len(mgr.commands),
                session_id,
            )
        except Exception as exc:
            logger.error("ACP: extension init failed for session %s: %s", session_id, exc)
        self._extension_managers[session_id] = mgr

    async def _setup_mcp(self, session_id: str, mcp_servers: list) -> None:
        """Convert ACP mcp_servers → MCPServerConfig, start bridge, register tools.

        Also installs the ``acp_terminal`` tool when a client is connected so
        the agent can run commands through the editor's terminal instead of a
        local subprocess.
        """
        from agent.tools.registry import ToolRegistry as TR

        registry = self._session_registries.get(session_id)
        if registry is None:
            registry = TR()

        if self._conn is not None and self._client_supports_terminal():
            from agent.tools.builtin.acp_terminal import register_acp_terminal_tool

            register_acp_terminal_tool(registry, self._conn, session_id)
            self._session_registries[session_id] = registry

        if not mcp_servers:
            return
        try:
            from agent.extensions.mcp import MCPBridge, MCPServerConfig

            configs: list[MCPServerConfig] = []
            for srv in mcp_servers:
                cfg = _acp_server_to_mcp_config(srv)
                if cfg:
                    configs.append(cfg)
            if not configs:
                return

            bridge = MCPBridge(configs)
            await bridge.__aenter__()
            count = await bridge.register_all(registry)
            self._mcp_bridges[session_id] = bridge
            self._session_registries[session_id] = registry
            logger.info(
                "ACP: registered %d MCP tool(s) from %d server(s) for session %s",
                count,
                len(configs),
                session_id,
            )
        except Exception as exc:
            logger.error("ACP: MCP setup failed for session %s: %s", session_id, exc, exc_info=True)
            self._notify_mcp_failure(session_id, exc)

    def _notify_mcp_failure(self, session_id: str, exc: BaseException) -> None:
        """Push a visible error to the client so the user sees MCP setup failed."""
        if self._conn is None:
            return
        try:
            from acp import text_block, update_agent_message
        except ImportError:
            return
        msg = f"MCP setup failed for this session: {exc}"
        self._spawn(
            self._conn.session_update(
                session_id=session_id,
                update=update_agent_message(text_block(msg)),
                source=self._agent_name,
            ),
            name=f"mcp-failure-{session_id}",
        )

    async def _teardown_mcp(self, session_id: str) -> None:
        bridge = self._mcp_bridges.pop(session_id, None)
        if bridge:
            try:
                await bridge.__aexit__(None, None, None)
            except Exception as exc:
                logger.warning("ACP: MCP teardown error for session %s: %s", session_id, exc)
        self._session_registries.pop(session_id, None)

    async def close_all_sessions(self) -> None:
        """Tear down every active session's per-session state.

        Called from :func:`run_acp_stdio`'s ``finally`` block so MCP
        subprocesses, async generators, and prompt tasks are closed in an
        orderly fashion *before* the event loop shuts down. Without this,
        asyncio's async-generator GC would close ``stdio_client`` from an
        arbitrary task, tripping anyio's same-task cancel-scope check::

            RuntimeError: Attempted to exit cancel scope in a different task
                          than it was entered in

        Best-effort — individual session failures are logged and skipped
        so one bad session can't block teardown of the others.
        """
        # Snapshot before iterating — _teardown_mcp mutates _mcp_bridges,
        # and close_session (if we ever route through it) mutates more.
        session_ids = list(self._sessions.keys() | self._mcp_bridges.keys())

        for sid in session_ids:
            # Cancel any prompt task still in flight so it doesn't hold a
            # reference to the MCP-backed tool registry while we close it.
            task = self._run_tasks.get(sid)
            event = self._cancel_events.get(sid)
            if event is not None:
                event.set()
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 — cleanup
                    pass

            try:
                await self._teardown_mcp(sid)
            except Exception as exc:  # noqa: BLE001 — best-effort shutdown
                logger.warning("ACP: shutdown teardown error for session %s: %s", sid, exc)

        # Flush any remaining fire-and-forget background tasks (session_update
        # notifications, command pushes, etc.).
        await self.shutdown()


async def run_acp_stdio(
    config: AgentConfig | None = None,
    approval_callback: ApprovalCallback | None = None,
    registry: ToolRegistry | None = None,
    agent_name: str = "aar",
) -> None:
    """Run the Aar ACP agent over stdio (SDK transport).

    Reads from stdin, writes to stdout. All other output (logging, errors)
    goes to stderr so it does not corrupt the JSON-RPC stream.
    """
    try:
        from acp import Agent as SdkAgent
        from acp import run_agent
    except ImportError as exc:
        raise ImportError(
            "agent-client-protocol is required for the ACP stdio transport. "
            "Install with: pip install agent-client-protocol"
        ) from exc

    # Dynamically create a proper subclass of the SDK's Agent base class.
    # AarAcpAgent must come first so our method implementations shadow the
    # Protocol stubs defined on SdkAgent (Agent Protocol).
    agent_cls = type(
        "AarSdkAgent",
        (AarAcpAgent, SdkAgent),
        {},
    )
    agent = agent_cls(
        config=config,
        approval_callback=approval_callback,
        registry=registry,
        agent_name=agent_name,
    )
    try:
        await run_agent(agent, use_unstable_protocol=True)
    finally:
        # Orderly teardown of MCP subprocesses + background tasks before the
        # event loop shuts down. Avoids the anyio cross-task cancel-scope
        # error that surfaces when async-generator GC runs after the loop
        # has started tearing tasks down.
        try:
            await agent.close_all_sessions()
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup on exit
            logger.warning("ACP: error during shutdown teardown: %s", exc)
