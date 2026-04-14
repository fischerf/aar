"""ACP transport — Agent Communication Protocol server for Aar.

Two transports in one module:

1. **Stdio (SDK-based)** — ``AarAcpAgent`` + ``run_acp_stdio()``
   Uses the official ``agent-client-protocol`` Python SDK.  This is what
   ``aar acp`` starts by default and what editors like Zed connect to via
   the ``"type": "custom"`` agent server setting.

2. **HTTP REST** — ``AcpTransport`` + ``create_acp_asgi_app()``
   A raw ASGI app that speaks ACP v0.2 over HTTP/SSE.  Useful for
   programmatic or remote use-cases where stdio is not available.

Quick reference
---------------
   aar acp                        # stdio (Zed, CLI orchestrators)
   aar acp --http                 # HTTP REST on 127.0.0.1:8000
   aar acp --http --port 9000     # custom port

Spec: https://agentcommunicationprotocol.dev
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
from agent.core.config import AgentConfig, load_config
from agent.core.events import (
    AssistantMessage,
    ErrorEvent,
    Event,
    ReasoningBlock,
    SessionEvent,
    StreamChunk,
    ToolCall,
    ToolResult,
)
from agent.core.session import Session
from agent.core.state import AgentState
from agent.memory.session_store import SessionStore
from agent.safety.permissions import ApprovalCallback, ApprovalResult
from agent.tools.registry import ToolRegistry
from agent.tools.schema import ToolSpec

logger = logging.getLogger(__name__)


def _load_default_config() -> AgentConfig:
    from pathlib import Path

    p = Path.home() / ".aar" / "config.json"
    return load_config(p) if p.is_file() else AgentConfig()


async def _auto_approve(spec: ToolSpec, tc: ToolCall) -> ApprovalResult:
    logger.info("ACP transport: auto-approving %s", tc.tool_name)
    return ApprovalResult.APPROVED


# ===========================================================================
# Part 1 — SDK-based stdio agent  (for Zed and any ACP-compatible editor)
# ===========================================================================


def _extract_text(prompt_blocks: list) -> str:
    """Pull plain text out of ACP SDK prompt content blocks.

    Handles TextContentBlock, ResourceContentBlock (URI links), and
    EmbeddedResourceContentBlock (@ file context with embedded content).
    """
    parts: list[str] = []
    for block in prompt_blocks:
        if isinstance(block, dict):
            btype = block.get("type", "")
            text = block.get("text", "")
            if text:
                parts.append(text)
            elif btype == "resource":
                uri = block.get("uri", "")
                if uri:
                    parts.append(f"[resource: {uri}]")
        else:
            # TextContentBlock
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
                continue
            # ResourceContentBlock — a URI link (e.g. a file path from @)
            uri = getattr(block, "uri", None)
            if uri:
                parts.append(f"[resource: {uri}]")
                continue
            # EmbeddedResourceContentBlock — actual file contents from @
            resource = getattr(block, "resource", None)
            if resource is not None:
                res_text = getattr(resource, "text", None)
                res_uri = getattr(resource, "uri", None)
                if res_text:
                    header = f"[{res_uri}]" if res_uri else "[file]"
                    parts.append(f"{header}\n{res_text}")
                elif res_uri:
                    parts.append(f"[resource: {res_uri}]")
    return "\n".join(parts)


def _map_stop_reason(state: AgentState) -> str:
    if state == AgentState.MAX_STEPS:
        return "max_turns"
    return "end_turn"


class AarAcpAgent:
    """Aar agent wrapped as an ACP stdio agent via the official SDK.

    Subclasses ``acp.Agent`` (from the ``agent-client-protocol`` package).
    Zed and other ACP-compatible editors communicate with this class over
    stdin/stdout using the SDK's framing — no HTTP server needed.

    Import lazily so the package is optional at import time.
    """

    # Real base class is set in __init_subclass__ trick below; we set it
    # dynamically in ``_build_class()`` to keep the import optional.

    def __init__(
        self,
        config: AgentConfig | None = None,
        approval_callback: ApprovalCallback | None = None,
        registry: ToolRegistry | None = None,
        agent_name: str = "aar",
    ) -> None:
        self._config = config or _load_default_config()
        self._approval_callback: ApprovalCallback = approval_callback or _auto_approve
        self._registry = registry
        self._agent_name = agent_name
        self._store = SessionStore(self._config.session_dir)
        self._sessions: dict[str, Session] = {}
        self._conn: Any = None  # acp.interfaces.Client, set in on_connect

    # ------------------------------------------------------------------
    # ACP SDK lifecycle hooks
    # ------------------------------------------------------------------

    def on_connect(self, conn: Any) -> None:
        self._conn = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any = None,
        client_info: Any = None,
        **kwargs: Any,
    ) -> Any:
        from acp import PROTOCOL_VERSION, InitializeResponse
        from acp.schema import AgentCapabilities, Implementation

        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(),
            agent_info=Implementation(name="aar", title="Aar Agent", version="0.3.2"),
        )

    async def new_session(
        self,
        cwd: str = "",
        mcp_servers: list | None = None,
        **kwargs: Any,
    ) -> Any:
        from acp import NewSessionResponse

        session = Session()
        self._sessions[session.session_id] = session
        self._store.save(session)
        logger.info("ACP: new session %s", session.session_id)
        return NewSessionResponse(session_id=session.session_id)

    async def load_session(
        self,
        cwd: str = "",
        session_id: str = "",
        mcp_servers: list | None = None,
        **kwargs: Any,
    ) -> Any:
        """Resume a previously saved session.  Returns None when not found."""
        from acp import LoadSessionResponse

        try:
            session = self._store.load(session_id)
            self._sessions[session_id] = session
            logger.info("ACP: loaded session %s (%d events)", session_id, len(session.events))
            return LoadSessionResponse()
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
        from acp.schema import ListSessionsResponse, SessionInfo

        session_infos: list[SessionInfo] = []
        for sid in self._store.list_sessions():
            try:
                s = self._store.load(sid)
                # Use first assistant message as title, fall back to ID prefix
                title = next(
                    (e.content[:60] for e in s.events if isinstance(e, AssistantMessage) and e.content),
                    sid[:12],
                )
                session_infos.append(SessionInfo(session_id=sid, cwd="", title=title))
            except Exception:
                session_infos.append(SessionInfo(session_id=sid, cwd="", title=sid[:12]))
        return ListSessionsResponse(sessions=session_infos)

    async def prompt(
        self,
        prompt: list,
        session_id: str,
        **kwargs: Any,
    ) -> Any:
        from acp import PromptResponse, text_block, update_agent_message
        from acp.schema import AgentThoughtChunk, TextContentBlock, ToolCallProgress, ToolCallStart

        text = _extract_text(prompt)

        # Restore session — in-memory cache → disk → fresh session with given ID
        session = self._sessions.get(session_id)
        if session is None:
            try:
                session = self._store.load(session_id)
                self._sessions[session_id] = session
            except (FileNotFoundError, ValueError):
                logger.info("ACP: creating fresh session for id %s", session_id)
                session = Session(session_id=session_id)
                self._sessions[session_id] = session

        aar_agent = self._make_aar_agent()
        update_tasks: list[asyncio.Task] = []

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

        def on_event(event: Event) -> None:
            if isinstance(event, AssistantMessage) and event.content:
                _push(update_agent_message(text_block(event.content)))

            elif isinstance(event, ReasoningBlock) and event.content:
                _push(
                    AgentThoughtChunk(
                        content=TextContentBlock(type="text", text=event.content),
                        session_update="agent_thought_chunk",
                    )
                )

            elif isinstance(event, ToolCall):
                _push(
                    ToolCallStart(
                        title=event.tool_name,
                        tool_call_id=event.tool_call_id or str(uuid.uuid4()),
                        status="in_progress",
                        raw_input=json.dumps(event.arguments, ensure_ascii=False),
                        session_update="tool_call",
                    )
                )

            elif isinstance(event, ToolResult):
                _push(
                    ToolCallProgress(
                        title=event.tool_name,
                        tool_call_id=event.tool_call_id,
                        status="failed" if event.is_error else "completed",
                        raw_output=event.output[:2000],
                        session_update="tool_call_update",
                    )
                )

        aar_agent.on_event(on_event)
        finished = await aar_agent.run(text, session)

        # Flush all pending session_update tasks before returning
        if update_tasks:
            await asyncio.gather(*update_tasks, return_exceptions=True)

        self._sessions[session_id] = finished
        self._store.save(finished)

        return PromptResponse(stop_reason=_map_stop_reason(finished.state))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_aar_agent(self) -> AarAgent:
        return AarAgent(
            config=self._config,
            approval_callback=self._approval_callback,
            registry=self._registry,
        )


async def run_acp_stdio(
    config: AgentConfig | None = None,
    approval_callback: ApprovalCallback | None = None,
    registry: ToolRegistry | None = None,
    agent_name: str = "aar",
) -> None:
    """Run the Aar ACP agent over stdio (SDK transport).

    Reads from stdin, writes to stdout.  All other output (logging, errors)
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
    await run_agent(agent)


# ===========================================================================
# Part 2 — HTTP REST transport  (ACP v0.2 over HTTP/SSE)
# ===========================================================================


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


AcpSseEvent = (
    RunCreatedEvent
    | MessageCreatedEvent
    | RunInProgressEvent
    | RunCompletedEvent
    | RunFailedEvent
    | RunCancelledEvent
)


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

    def get_manifest(self) -> AgentManifest:
        return AgentManifest(
            name=self.agent_name,
            description=self.agent_description,
            input_content_types=["text/plain"],
            output_content_types=["text/plain"],
            metadata={
                "provider": self.config.provider.name,
                "model": self.config.provider.model,
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

        try:
            aar_agent = self._make_agent()
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

            aar_agent.on_event(on_event)

            session: Session | None = None
            if session_id:
                try:
                    session = self.store.load(session_id)
                except FileNotFoundError:
                    pass

            finished = await aar_agent.run(prompt, session, cancel_event=record.cancel_event)
            self.store.save(finished)
            run.session_id = finished.session_id

            if queue and _stream_buf:
                _flush_buf()

            if not queue:
                run.output = _collect_output(finished)

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
            if queue:
                await queue.put(None)

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
            except (asyncio.CancelledError, Exception):
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
        except FileNotFoundError:
            return None

    def _make_agent(self) -> AarAgent:
        return AarAgent(
            config=self.config,
            approval_callback=self.approval_callback,
            registry=self.registry,
        )


def _collect_output(session: Session) -> list[AcpMessage]:
    return [
        AcpMessage.from_text("assistant", e.content)
        for e in session.events
        if isinstance(e, AssistantMessage) and e.content
    ]


# ---------------------------------------------------------------------------
# Minimal ASGI application (HTTP REST)
# ---------------------------------------------------------------------------


def create_acp_asgi_app(
    config: AgentConfig | None = None,
    approval_callback: ApprovalCallback | None = None,
    registry: ToolRegistry | None = None,
    agent_name: str = "aar",
    agent_description: str = "Aar adaptive action & reasoning agent",
) -> Any:
    """Create a minimal ASGI app that speaks the ACP v0.2 HTTP/SSE protocol.

    Use this for programmatic or remote access.  For Zed and other editors
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
    GET  /ping                    — health check
    """
    transport = AcpTransport(
        config=config,
        approval_callback=approval_callback,
        registry=registry,
        agent_name=agent_name,
        agent_description=agent_description,
    )

    async def app(scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        path: str = scope["path"]
        method: str = scope["method"]

        if method == "OPTIONS":
            await _cors_preflight(send)
            return

        if method == "GET" and path == "/ping":
            await _json(send, {"status": "ok"})

        elif method == "GET" and path == "/agents":
            await _json(send, {"agents": [transport.get_manifest().model_dump()]})

        elif method == "GET" and path.startswith("/agents/"):
            name = path[len("/agents/"):]
            if name == transport.agent_name:
                await _json(send, transport.get_manifest().model_dump())
            else:
                await _json(send, {"detail": f"Agent '{name}' not found"}, status=404)

        elif method == "POST" and path == "/runs":
            body = await _read_body(receive)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                await _json(send, {"detail": "Invalid JSON"}, status=400)
                return
            try:
                mode = RunMode(data.get("mode", "sync"))
            except ValueError:
                await _json(send, {"detail": f"Invalid mode: {data.get('mode')!r}"}, status=400)
                return
            try:
                msgs = [AcpMessage.model_validate(m) for m in data.get("input", [])]
            except Exception as exc:
                await _json(send, {"detail": f"Invalid input: {exc}"}, status=400)
                return
            try:
                run, queue = await transport.create_run(
                    agent_name=data.get("agent_name", transport.agent_name),
                    input_messages=msgs,
                    mode=mode,
                    session_id=data.get("session_id"),
                )
            except ValueError as exc:
                await _json(send, {"detail": str(exc)}, status=404)
                return
            if mode == RunMode.STREAM and queue is not None:
                await _sse_run_stream(send, queue)
            elif mode == RunMode.ASYNC:
                await _json(send, run.model_dump(), status=202)
            else:
                await _json(send, run.model_dump())

        elif method == "GET" and _matches(path, "/runs/", 1):
            run_id = _path_tail(path, "/runs/")
            if "/" in run_id:
                await _json(send, {"detail": "Not found"}, status=404)
                return
            run = transport.get_run(run_id)
            if run:
                await _json(send, run.model_dump())
            else:
                await _json(send, {"detail": f"Run '{run_id}' not found"}, status=404)

        elif method == "POST" and path.endswith("/cancel") and "/runs/" in path:
            run_id = path[len("/runs/"):].removesuffix("/cancel")
            run = await transport.cancel_run(run_id)
            if run:
                await _json(send, run.model_dump())
            else:
                await _json(send, {"detail": f"Run '{run_id}' not found"}, status=404)

        elif method == "GET" and path.endswith("/events") and "/runs/" in path:
            run_id = path[len("/runs/"):].removesuffix("/events")
            events = transport.get_run_events(run_id)
            if events is not None:
                await _json(send, {"events": [e.model_dump() for e in events]})
            else:
                await _json(send, {"detail": f"Run '{run_id}' not found"}, status=404)

        elif method == "POST" and _matches(path, "/runs/", 1):
            run_id = _path_tail(path, "/runs/")
            run = transport.get_run(run_id)
            if run:
                await _json(send, {"detail": "Resume not supported; run is not awaiting"}, status=422)
            else:
                await _json(send, {"detail": f"Run '{run_id}' not found"}, status=404)

        elif method == "GET" and path.startswith("/sessions/"):
            sid = path[len("/sessions/"):]
            info = transport.get_session(sid)
            if info:
                await _json(send, info)
            else:
                await _json(send, {"detail": f"Session '{sid}' not found"}, status=404)

        else:
            await _json(send, {"detail": "Not found"}, status=404)

    return app


# ---------------------------------------------------------------------------
# ASGI helpers
# ---------------------------------------------------------------------------


def _matches(path: str, prefix: str, min_segments: int) -> bool:
    if not path.startswith(prefix):
        return False
    return len(path[len(prefix):].split("/")) >= min_segments


def _path_tail(path: str, prefix: str) -> str:
    return path[len(prefix):]


async def _read_body(receive: Any) -> bytes:
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if not msg.get("more_body", False):
            break
    return body


_CORS = [
    [b"access-control-allow-origin", b"*"],
    [b"access-control-allow-methods", b"GET, POST, OPTIONS"],
    [b"access-control-allow-headers", b"content-type"],
]


async def _cors_preflight(send: Any) -> None:
    await send({"type": "http.response.start", "status": 204, "headers": _CORS})
    await send({"type": "http.response.body", "body": b""})


async def _json(send: Any, data: dict, status: int = 200) -> None:
    body = json.dumps(data).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(body)).encode()],
                *_CORS,
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _sse_run_stream(
    send: Any,
    queue: asyncio.Queue[AcpSseEvent | None],
) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                [b"content-type", b"text/event-stream"],
                [b"cache-control", b"no-cache"],
                [b"connection", b"keep-alive"],
                *_CORS,
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
