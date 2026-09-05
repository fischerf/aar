"""ACP HTTP/SSE transport — ACP v0.2 over HTTP for programmatic clients.

A raw ASGI app speaking the ACP v0.2 REST + SSE protocol. Useful when
stdio is not available (remote orchestrators, test harnesses, browsers).

For Zed and other editors that launch the agent as a child process, use
``run_acp_stdio()`` from ``agent.transports.acp.stdio`` instead.

.. warning::

   **The HTTP transport implements only the run-execution subset of
   the ACP protocol.** It is feature-incomplete compared to the stdio
   transport (``agent.transports.acp.stdio``) and is intended for
   simple programmatic / curl-based use only.

   Missing — relative to stdio — at the time of writing (Wave 4 audit,
   2026-06):

   * **MCP bridge** — editor-provided MCP servers are not wired in,
     so tools exposed via ``initialize.mcp_servers`` are unavailable.
   * **Slash commands** — ``/model``, ``/help``, ``/clear`` etc. are
     not parsed; user input is forwarded verbatim to the model.
   * **Extensions** — loaded per *session* (one cached ``Agent`` per
     ``session_id``), so ``register(api)`` hooks, custom tools, prompt
     contributions and UI panels apply and keep their state across runs.
     Extension slash commands are still **not** parsed on this transport.
     Panels: ``GET /sessions/{id}/panels``, ``GET /sessions/{id}/panels/{name}``,
     ``POST /sessions/{id}/panels/{name}/actions/{action}`` and the
     ``panel_changed`` SSE event.
   * **ACP permission bridging** — approval requests fall back to
     the auto-approve callback; there is no ``session/request_permission``
     round-trip to the client.
   * **``session_update`` replay** — assistant message chunks are
     buffered and only delivered via the HTTP run object; ACP
     ``session/update`` notifications used by IDEs are not emitted.
   * **``set_session_model``** — clients cannot switch provider/model
     mid-session over HTTP.
   * **Session fork / resume / list** — only ``GET /sessions/{id}``
     metadata is exposed; no ACP ``session/load`` semantics, fork, or
     listing endpoints are implemented.

   If you need any of the above, run the stdio transport behind your
   own process supervisor instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent.core.agent import Agent as AarAgent
from agent.core.config import AgentConfig
from agent.core.events import AssistantMessage, ContextWindowEvent, Event, StreamChunk
from agent.core.session import Session
from agent.core.state import AgentState
from agent.memory.session_store import SessionStore
from agent.safety.permissions import ApprovalCallback
from agent.tools.registry import ToolRegistry

from agent.transports._http_auth import (
    PUBLIC_PATHS,
    BearerAuth,
    cors_headers,
    normalize_origins,
)

from .common import _auto_approve, _deny_approval, _load_default_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ACP HTTP data models
# ---------------------------------------------------------------------------


class RunStatus(str, Enum):
    """Lifecycle states for an ACP run."""

    CREATED = "created"
    IN_PROGRESS = "in-progress"
    AWAITING = "awaiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunMode(str, Enum):
    """How the client wants to receive the result."""

    SYNC = "sync"
    ASYNC = "async"
    STREAM = "stream"


class MessagePart(BaseModel):
    """A single content part within an ACP message."""

    content_type: str = "text/plain"
    content: str = ""


class AcpMessage(BaseModel):
    """An ACP message — analogous to a chat turn."""

    role: str  # "user" | "assistant"
    parts: list[MessagePart] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(p.content for p in self.parts if p.content_type == "text/plain")

    @classmethod
    def from_text(cls, role: str, text: str) -> "AcpMessage":
        return cls(role=role, parts=[MessagePart(content_type="text/plain", content=text)])


class AcpRun(BaseModel):
    """Serialisable state for an ACP run."""

    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    agent_name: str = ""
    status: RunStatus = RunStatus.CREATED
    session_id: str | None = None
    output: list[AcpMessage] = Field(default_factory=list)
    error: str | None = None
    created_at: str = Field(default_factory=lambda: _now_iso())
    finished_at: str | None = None
    # Context-window fill — updated live during streaming runs
    ctx_tokens: int = 0
    ctx_window: int = 0
    msgs_dropped: int = 0

    def finish(self, status: RunStatus, error: str | None = None) -> None:
        self.status = status
        self.finished_at = _now_iso()
        if error:
            self.error = error


class AgentManifest(BaseModel):
    """ACP agent manifest."""

    name: str
    description: str
    input_content_types: list[str] = Field(default_factory=lambda: ["text/plain"])
    output_content_types: list[str] = Field(default_factory=lambda: ["text/plain"])
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# ACP SSE event envelopes
# ---------------------------------------------------------------------------


class RunCreatedEvent(BaseModel):
    type: Literal["run_created"] = "run_created"
    run: AcpRun


class MessageCreatedEvent(BaseModel):
    type: Literal["message_created"] = "message_created"
    message: AcpMessage


class RunInProgressEvent(BaseModel):
    type: Literal["run_in_progress"] = "run_in_progress"
    run: AcpRun


class RunCompletedEvent(BaseModel):
    type: Literal["run_completed"] = "run_completed"
    run: AcpRun


class RunFailedEvent(BaseModel):
    type: Literal["run_failed"] = "run_failed"
    run: AcpRun


class RunCancelledEvent(BaseModel):
    type: Literal["run_cancelled"] = "run_cancelled"
    run: AcpRun


class ContextWindowUpdatedEvent(BaseModel):
    """SSE event emitted each turn when context-window management trims the history."""

    type: Literal["context_window_updated"] = "context_window_updated"
    run_id: str
    ctx_tokens: int = 0
    ctx_window: int = 0
    msgs_dropped: int = 0
    msgs_before: int = 0
    msgs_after: int = 0
    strategy: str = ""


class PanelChangedEvent(BaseModel):
    """An extension UI panel flagged its state as stale during the run.

    Fetch ``GET /sessions/{session_id}/panels/{panel}`` for a fresh tree.
    """

    type: Literal["panel_changed"] = "panel_changed"
    run_id: str
    session_id: str
    panel: str


AcpSseEvent = (
    RunCreatedEvent
    | MessageCreatedEvent
    | RunInProgressEvent
    | RunCompletedEvent
    | RunFailedEvent
    | RunCancelledEvent
    | ContextWindowUpdatedEvent
    | PanelChangedEvent
)


class HttpError(Exception):
    """Raised by transport methods; the ASGI layer maps it to a JSON reply."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ---------------------------------------------------------------------------
# Internal run record
# ---------------------------------------------------------------------------


class _RunRecord:
    def __init__(self, run: AcpRun) -> None:
        self.run = run
        self.cancel_event: asyncio.Event = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.acp_events: list[AcpSseEvent] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sse_line(obj: BaseModel) -> bytes:
    return f"data: {obj.model_dump_json()}\n\n".encode()


def _collect_output(session: Session) -> list[AcpMessage]:
    return [
        AcpMessage.from_text("assistant", e.content)
        for e in session.events
        if isinstance(e, AssistantMessage) and e.content
    ]


# ---------------------------------------------------------------------------
# AcpTransport — HTTP backend
# ---------------------------------------------------------------------------


class AcpTransport:
    """Bridges Aar's agent runtime to the ACP v0.2 HTTP/SSE protocol."""

    def __init__(
        self,
        config: AgentConfig | None = None,
        approval_callback: ApprovalCallback | None = None,
        registry: ToolRegistry | None = None,
        agent_name: str = "aar",
        agent_description: str = "Aar adaptive action & reasoning agent",
    ) -> None:
        self.config = config or _load_default_config()
        self.approval_callback: ApprovalCallback = approval_callback or _auto_approve
        self.registry = registry
        self.agent_name = agent_name
        self.agent_description = agent_description
        self.store = SessionStore(self.config.session_dir)
        self._runs: dict[str, _RunRecord] = {}
        # One Agent per session so extension state (hooks, UI panels) survives
        # across runs instead of being rebuilt — and discarded — every run.
        self._agents: dict[str, AarAgent] = {}
        # #3b — Surface the feature gap vs the stdio transport on every
        # construction so operators see it in their logs. See module
        # docstring above for the full list.
        logger.warning(
            "ACP HTTP transport is feature-incomplete vs stdio: no MCP "
            "bridge, slash commands, ACP permission requests, "
            "session_update replay, set_session_model, or session "
            "fork/resume/list. See agent.transports.acp.http module "
            "docstring for the full list."
        )

    def get_manifest(self) -> AgentManifest:
        return AgentManifest(
            name=self.agent_name,
            description=self.agent_description,
            input_content_types=["text/plain"],
            output_content_types=["text/plain"],
            metadata={
                "provider": self.config.resolve_provider().name,
                "model": self.config.resolve_provider().model,
                "max_steps": self.config.max_steps,
            },
        )

    async def create_run(
        self,
        agent_name: str,
        input_messages: list[AcpMessage],
        mode: RunMode,
        session_id: str | None = None,
    ) -> tuple[AcpRun, asyncio.Queue[AcpSseEvent | None] | None]:
        if agent_name != self.agent_name:
            raise ValueError(f"Unknown agent: {agent_name!r}")

        prompt = "\n".join(m.text for m in input_messages if m.role == "user") or ""
        run = AcpRun(agent_name=agent_name, status=RunStatus.CREATED, session_id=session_id)
        record = _RunRecord(run)
        self._runs[run.run_id] = record
        record.acp_events.append(RunCreatedEvent(run=run))

        if mode == RunMode.SYNC:
            await self._execute_run(record, prompt, session_id, queue=None)
            return run, None

        if mode == RunMode.ASYNC:
            run.status = RunStatus.IN_PROGRESS
            record.task = asyncio.create_task(
                self._execute_run(record, prompt, session_id, queue=None)
            )
            return run, None

        queue: asyncio.Queue[AcpSseEvent | None] = asyncio.Queue()
        run.status = RunStatus.IN_PROGRESS
        record.task = asyncio.create_task(
            self._execute_run(record, prompt, session_id, queue=queue)
        )
        return run, queue

    async def _execute_run(
        self,
        record: _RunRecord,
        prompt: str,
        session_id: str | None,
        queue: asyncio.Queue[AcpSseEvent | None] | None,
    ) -> None:
        run = record.run
        run.status = RunStatus.IN_PROGRESS
        in_progress_evt = RunInProgressEvent(run=run.model_copy())
        record.acp_events.append(in_progress_evt)
        if queue:
            await queue.put(in_progress_evt)

        aar_agent: AarAgent | None = None
        on_event: Any = None
        try:
            aar_agent = self._agent_for(session_id) if session_id else self._make_agent()
            _stream_buf: list[str] = []

            def _flush_buf() -> None:
                nonlocal _stream_buf
                if _stream_buf and queue:
                    text = "".join(_stream_buf)
                    msg = AcpMessage.from_text("assistant", text)
                    evt = MessageCreatedEvent(message=msg)
                    record.acp_events.append(evt)
                    queue.put_nowait(evt)
                _stream_buf = []

            def on_event(event: Event) -> None:
                if isinstance(event, StreamChunk) and not event.finished and event.text:
                    _stream_buf.append(event.text)
                elif isinstance(event, AssistantMessage) and event.content:
                    if queue:
                        _flush_buf()
                        msg = AcpMessage.from_text("assistant", event.content)
                        evt = MessageCreatedEvent(message=msg)
                        record.acp_events.append(evt)
                        queue.put_nowait(evt)
                    else:
                        run.output.append(AcpMessage.from_text("assistant", event.content))
                elif isinstance(event, ContextWindowEvent):
                    # Update run metadata so REST pollers see the latest fill state.
                    run.ctx_tokens = event.ctx_tokens
                    run.ctx_window = event.ctx_window
                    run.msgs_dropped = event.msgs_dropped
                    if queue:
                        cw_evt = ContextWindowUpdatedEvent(
                            run_id=run.run_id,
                            ctx_tokens=event.ctx_tokens,
                            ctx_window=event.ctx_window,
                            msgs_dropped=event.msgs_dropped,
                            msgs_before=event.msgs_before,
                            msgs_after=event.msgs_after,
                            strategy=event.strategy,
                        )
                        record.acp_events.append(cw_evt)
                        queue.put_nowait(cw_evt)

            aar_agent.on_event(on_event)

            session: Session | None = None
            if session_id:
                try:
                    session = self.store.load(session_id)
                except (FileNotFoundError, ValueError):
                    pass

            finished = await aar_agent.run(prompt, session, cancel_event=record.cancel_event)
            self.store.save(finished)
            run.session_id = finished.session_id
            # A run without session_id created the session — keep its agent so
            # follow-up runs and panel requests see the same extension state.
            self._agents.setdefault(finished.session_id, aar_agent)

            if queue and _stream_buf:
                _flush_buf()

            if not queue:
                run.output = _collect_output(finished)

            for panel_evt in self._drain_panel_changes(run.run_id, aar_agent, finished.session_id):
                record.acp_events.append(panel_evt)
                if queue:
                    queue.put_nowait(panel_evt)

            if finished.state == AgentState.CANCELLED:
                run.finish(RunStatus.CANCELLED)
                evt: AcpSseEvent = RunCancelledEvent(run=run.model_copy())
            else:
                run.finish(RunStatus.COMPLETED)
                evt = RunCompletedEvent(run=run.model_copy())

            record.acp_events.append(evt)
            if queue:
                await queue.put(evt)

        except asyncio.CancelledError:
            run.finish(RunStatus.CANCELLED)
            evt = RunCancelledEvent(run=run.model_copy())
            record.acp_events.append(evt)
            if queue:
                await queue.put(evt)
            raise

        except Exception as exc:
            logger.exception("ACP HTTP run %s failed", run.run_id)
            run.finish(RunStatus.FAILED, error=str(exc))
            evt = RunFailedEvent(run=run.model_copy())
            record.acp_events.append(evt)
            if queue:
                await queue.put(evt)

        finally:
            # The agent is reused across runs — drop this run's listener.
            if aar_agent is not None and on_event is not None:
                aar_agent.off_event(on_event)
            if queue:
                await queue.put(None)

    # ------------------------------------------------------------------
    # Extension UI panels (see agent.extensions.api.UIPanel)
    # ------------------------------------------------------------------

    def _agent_for(self, session_id: str) -> AarAgent:
        """The cached Agent for *session_id*, created on first use."""
        agent = self._agents.get(session_id)
        if agent is None:
            agent = self._make_agent()
            self._agents[session_id] = agent
        return agent

    async def _extension_manager_for(self, session_id: str) -> Any:
        """Extension manager for *session_id*, initialising it if the session
        has not run on this transport yet (e.g. resumed from disk)."""
        try:
            session = self.store.load(session_id)
        except (FileNotFoundError, ValueError):
            if session_id not in self._agents:
                raise HttpError(404, f"Session '{session_id}' not found") from None
            session = Session(session_id=session_id)
        agent = self._agent_for(session_id)
        if agent._extension_manager is None:
            await agent._init_extensions(session)
        mgr = agent._extension_manager
        if mgr is None:
            raise HttpError(503, "extensions unavailable")
        mgr.update_session(session)
        return mgr

    def session_busy(self, session_id: str) -> bool:
        """True while a run for *session_id* is in progress."""
        return any(
            rec.run.session_id == session_id and rec.run.status == RunStatus.IN_PROGRESS
            for rec in self._runs.values()
        )

    @staticmethod
    def _drain_panel_changes(
        run_id: str, agent: AarAgent, session_id: str
    ) -> list[PanelChangedEvent]:
        mgr = getattr(agent, "_extension_manager", None)
        panels = getattr(mgr, "panels", None) if mgr is not None else None
        if not isinstance(panels, dict):
            return []
        out: list[PanelChangedEvent] = []
        for panel in panels.values():
            if panel.changed.is_set():
                panel.changed.clear()
                out.append(
                    PanelChangedEvent(run_id=run_id, session_id=session_id, panel=panel.name)
                )
        return out

    async def panel_list(self, session_id: str) -> dict[str, Any]:
        mgr = await self._extension_manager_for(session_id)
        ctx = mgr._context
        return {
            "session_id": session_id,
            "panels": [
                {
                    "name": p.name,
                    "title": p.title,
                    "status": p.status_text(ctx),
                    "actions": [a.to_dict() for a in p.actions],
                }
                for p in mgr.panels.values()
            ],
        }

    async def _panel(self, session_id: str, name: str) -> tuple[Any, Any]:
        mgr = await self._extension_manager_for(session_id)
        panel = mgr.panels.get(name)
        if panel is None:
            raise HttpError(404, f"Panel '{name}' not found")
        return mgr, panel

    async def panel_snapshot(self, session_id: str, name: str) -> dict[str, Any]:
        from agent.extensions.api import run_ui_snapshot

        mgr, panel = await self._panel(session_id, name)
        root = await run_ui_snapshot(panel, mgr._context)
        panel.changed.clear()
        return {
            "session_id": session_id,
            "panel": panel.name,
            "root": root.to_dict(),
            "status": panel.status_text(mgr._context),
        }

    async def panel_action(
        self,
        session_id: str,
        name: str,
        action_id: str,
        node_id: str | None,
        args: dict[str, Any] | None,
    ) -> dict[str, Any]:
        from agent.extensions.api import UIInvocation, run_ui_action, run_ui_snapshot

        mgr, panel = await self._panel(session_id, name)
        action = panel.action(action_id)
        if action is None:
            raise HttpError(404, f"Action '{action_id}' not found on panel '{name}'")
        if not node_id:
            raise HttpError(400, "node_id is required")
        ctx = mgr._context
        root = await run_ui_snapshot(panel, ctx)
        node = root.find(node_id)
        if node is None:
            raise HttpError(404, f"Node '{node_id}' not found")
        if not action.applies_to(node):
            raise HttpError(422, f"Action '{action_id}' does not apply to node kind '{node.kind}'")
        if action.mutates and self.session_busy(session_id):
            raise HttpError(409, "A run is in progress for this session — cancel it first")
        try:
            message = await run_ui_action(
                action, UIInvocation(node=node, ctx=ctx, args=dict(args or {}))
            )
        except Exception as exc:
            logger.error("ACP HTTP: panel action %s/%s failed: %s", name, action_id, exc)
            message = f"✗ {action_id}: {exc}"
        root = await run_ui_snapshot(panel, ctx)
        panel.changed.clear()
        if action.mutates:
            session = getattr(ctx, "session", None)
            if isinstance(session, Session):
                self.store.save(session)
        return {
            "session_id": session_id,
            "panel": panel.name,
            "action": action.id,
            "message": message,
            "root": root.to_dict(),
            "status": panel.status_text(ctx),
        }

    def get_run(self, run_id: str) -> AcpRun | None:
        record = self._runs.get(run_id)
        return record.run if record else None

    async def cancel_run(self, run_id: str) -> AcpRun | None:
        record = self._runs.get(run_id)
        if not record:
            return None
        record.cancel_event.set()
        if record.task and not record.task.done():
            record.task.cancel()
            try:
                await record.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — cleanup path
                pass
        if record.run.status not in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
            record.run.finish(RunStatus.CANCELLED)
        return record.run

    def get_run_events(self, run_id: str) -> list[AcpSseEvent] | None:
        record = self._runs.get(run_id)
        return record.acp_events if record else None

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        try:
            session = self.store.load(session_id)
            return {
                "session_id": session.session_id,
                "state": session.state.value,
                "step_count": session.step_count,
                "event_count": len(session.events),
            }
        except (FileNotFoundError, ValueError):
            return None

    def _make_agent(self, provider_key: str | None = None) -> AarAgent:
        config = self.config
        if provider_key:
            try:
                provider_cfg = config.resolve_provider(provider_key)
                config = config.model_copy(update={"provider": provider_cfg})
            except ValueError:
                pass  # fall through to default
        return AarAgent(
            config=config,
            approval_callback=self.approval_callback,
            registry=self.registry,
        )


# ---------------------------------------------------------------------------
# Minimal ASGI application (HTTP REST)
# ---------------------------------------------------------------------------


def create_acp_asgi_app(
    config: AgentConfig | None = None,
    approval_callback: ApprovalCallback | None = None,
    registry: ToolRegistry | None = None,
    agent_name: str = "aar",
    agent_description: str = "Aar adaptive action & reasoning agent",
    *,
    auth: BearerAuth | None = None,
    cors_origins: list[str] | None = None,
) -> Any:
    """Create a minimal ASGI app that speaks the ACP v0.2 HTTP/SSE protocol.

    Use this for programmatic or remote access. For Zed and other editors
    that launch the agent as a child process, use ``run_acp_stdio()`` instead.

    Endpoints
    ---------
    GET  /agents                  — list agents
    GET  /agents/{name}           — agent manifest
    POST /runs                    — create run (sync|async|stream)
    GET  /runs/{run_id}           — run status
    POST /runs/{run_id}           — resume run (reserved)
    POST /runs/{run_id}/cancel    — cancel run
    GET  /runs/{run_id}/events    — ACP event log
    GET  /sessions/{session_id}   — session metadata
    GET  /sessions/{id}/panels                  — extension UI panels + actions
    GET  /sessions/{id}/panels/{name}           — panel snapshot (UINode tree)
    POST /sessions/{id}/panels/{name}/actions/{action}
                                  — run an action; body {node_id, args?}
    GET  /ping                    — health check (the only unauthenticated route)

    C1 — Every other route requires ``Authorization: Bearer <token>``.  With
    no *auth* argument a token is generated; read it from ``app.auth.token``.
    Cross-origin requests are refused unless the origin is listed in
    *cors_origins*.
    """
    transport = AcpTransport(
        config=config,
        approval_callback=(approval_callback if approval_callback is not None else _deny_approval),
        registry=registry,
        agent_name=agent_name,
        agent_description=agent_description,
    )

    resolved_auth = auth if auth is not None else BearerAuth()
    allowed_origins = normalize_origins(cors_origins)

    async def app(scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        path: str = scope["path"]
        method: str = scope["method"]
        cors = cors_headers(scope, allowed_origins)

        async def _reply(data: dict, status: int = 200) -> None:
            await _json(send, data, status=status, cors=cors)

        if method == "OPTIONS":
            await _cors_preflight(send, cors)
            return

        # C1 — Authenticate before routing; ``/ping`` stays open for liveness.
        if path not in PUBLIC_PATHS and not resolved_auth.check(scope):
            await _reply({"detail": "Unauthorized"}, status=401)
            return

        if method == "GET" and path == "/ping":
            await _reply({"status": "ok"})

        elif method == "GET" and path == "/agents":
            await _reply({"agents": [transport.get_manifest().model_dump()]})

        elif method == "GET" and path.startswith("/agents/"):
            name = path[len("/agents/") :]
            if name == transport.agent_name:
                await _reply(transport.get_manifest().model_dump())
            else:
                await _reply({"detail": f"Agent '{name}' not found"}, status=404)

        elif method == "POST" and path == "/runs":
            body = await _read_body(receive)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                await _reply({"detail": "Invalid JSON"}, status=400)
                return
            try:
                mode = RunMode(data.get("mode", "sync"))
            except ValueError:
                await _reply({"detail": f"Invalid mode: {data.get('mode')!r}"}, status=400)
                return
            try:
                msgs = [AcpMessage.model_validate(m) for m in data.get("input", [])]
            except Exception as exc:
                await _reply({"detail": f"Invalid input: {exc}"}, status=400)
                return
            try:
                run, queue = await transport.create_run(
                    agent_name=data.get("agent_name", transport.agent_name),
                    input_messages=msgs,
                    mode=mode,
                    session_id=data.get("session_id"),
                )
            except ValueError as exc:
                await _reply({"detail": str(exc)}, status=404)
                return
            if mode == RunMode.STREAM and queue is not None:
                await _sse_run_stream(send, queue, cors)
            elif mode == RunMode.ASYNC:
                await _reply(run.model_dump(), status=202)
            else:
                await _reply(run.model_dump())

        # ``/runs/{id}/events`` must be matched before the generic
        # ``/runs/{id}`` branch below, which rejects any id containing ``/``.
        elif method == "GET" and path.endswith("/events") and "/runs/" in path:
            run_id = path[len("/runs/") :].removesuffix("/events")
            events = transport.get_run_events(run_id)
            if events is not None:
                await _reply({"events": [e.model_dump() for e in events]})
            else:
                await _reply({"detail": f"Run '{run_id}' not found"}, status=404)

        elif method == "GET" and _matches(path, "/runs/", 1):
            run_id = _path_tail(path, "/runs/")
            if "/" in run_id:
                await _reply({"detail": "Not found"}, status=404)
                return
            run = transport.get_run(run_id)
            if run:
                await _reply(run.model_dump())
            else:
                await _reply({"detail": f"Run '{run_id}' not found"}, status=404)

        elif method == "POST" and path.endswith("/cancel") and "/runs/" in path:
            run_id = path[len("/runs/") :].removesuffix("/cancel")
            run = await transport.cancel_run(run_id)
            if run:
                await _reply(run.model_dump())
            else:
                await _reply({"detail": f"Run '{run_id}' not found"}, status=404)

        elif method == "POST" and _matches(path, "/runs/", 1):
            run_id = _path_tail(path, "/runs/")
            run = transport.get_run(run_id)
            if run:
                await _reply({"detail": "Resume not supported; run is not awaiting"}, status=422)
            else:
                await _reply({"detail": f"Run '{run_id}' not found"}, status=404)

        elif "/panels" in path and path.startswith("/sessions/"):
            # /sessions/{sid}/panels
            # /sessions/{sid}/panels/{name}
            # /sessions/{sid}/panels/{name}/actions/{action}
            parts = path[len("/sessions/") :].split("/")
            sid = parts[0]
            try:
                if method == "GET" and parts[1:] == ["panels"]:
                    await _reply(await transport.panel_list(sid))
                elif method == "GET" and len(parts) == 3 and parts[1] == "panels":
                    await _reply(await transport.panel_snapshot(sid, parts[2]))
                elif (
                    method == "POST"
                    and len(parts) == 5
                    and parts[1] == "panels"
                    and parts[3] == "actions"
                ):
                    body = await _read_body(receive)
                    try:
                        data = json.loads(body) if body else {}
                    except json.JSONDecodeError:
                        await _reply({"detail": "Invalid JSON"}, status=400)
                        return
                    if not isinstance(data, dict):
                        await _reply({"detail": "Body must be a JSON object"}, status=400)
                        return
                    node_id = data.get("node_id") or data.get("nodeId")
                    args = data.get("args")
                    await _reply(
                        await transport.panel_action(
                            sid,
                            parts[2],
                            parts[4],
                            node_id if isinstance(node_id, str) else None,
                            args if isinstance(args, dict) else None,
                        )
                    )
                else:
                    await _reply({"detail": "Not found"}, status=404)
            except HttpError as exc:
                await _reply({"detail": exc.detail}, status=exc.status)

        elif method == "GET" and path.startswith("/sessions/"):
            sid = path[len("/sessions/") :]
            info = transport.get_session(sid)
            if info:
                await _reply(info)
            else:
                await _reply({"detail": f"Session '{sid}' not found"}, status=404)

        else:
            await _reply({"detail": "Not found"}, status=404)

    app.auth = resolved_auth  # type: ignore[attr-defined]
    app.transport = transport  # type: ignore[attr-defined]
    return app


# ---------------------------------------------------------------------------
# ASGI helpers
# ---------------------------------------------------------------------------


def _matches(path: str, prefix: str, min_segments: int) -> bool:
    if not path.startswith(prefix):
        return False
    return len(path[len(prefix) :].split("/")) >= min_segments


def _path_tail(path: str, prefix: str) -> str:
    return path[len(prefix) :]


async def _read_body(receive: Any) -> bytes:
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if not msg.get("more_body", False):
            break
    return body


async def _cors_preflight(send: Any, cors: list[list[bytes]] | None = None) -> None:
    await send({"type": "http.response.start", "status": 204, "headers": list(cors or [])})
    await send({"type": "http.response.body", "body": b""})


async def _json(
    send: Any,
    data: dict,
    status: int = 200,
    cors: list[list[bytes]] | None = None,
) -> None:
    body = json.dumps(data).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(body)).encode()],
                *(cors or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _sse_run_stream(
    send: Any,
    queue: asyncio.Queue[AcpSseEvent | None],
    cors: list[list[bytes]] | None = None,
) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                [b"content-type", b"text/event-stream"],
                [b"cache-control", b"no-cache"],
                [b"connection", b"keep-alive"],
                *(cors or []),
            ],
        }
    )
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            await send({"type": "http.response.body", "body": _sse_line(event), "more_body": True})
    finally:
        await send({"type": "http.response.body", "body": b"", "more_body": False})
