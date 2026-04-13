"""ACP transport — Agent Communication Protocol (v0.2) server for Aar.

Exposes Aar as a standards-compliant ACP agent.  Implements all required
REST endpoints plus SSE streaming so any ACP-compatible orchestrator or
client can discover and drive the agent.

Spec: https://agentcommunicationprotocol.dev

Endpoints
---------
GET  /agents                       — list registered agents
GET  /agents/{name}                — agent manifest
POST /runs                         — create a run (sync / async / stream)
GET  /runs/{run_id}                — run status + output
POST /runs/{run_id}                — resume an awaiting run
POST /runs/{run_id}/cancel         — cancel an in-progress run
GET  /runs/{run_id}/events         — list all ACP events for a run
GET  /sessions/{session_id}        — session metadata
GET  /ping                         — health check
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

from agent.core.agent import Agent
from agent.core.config import AgentConfig, load_config
from agent.core.events import (
    AssistantMessage,
    ErrorEvent,
    Event,
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

_USER_CONFIG = __import__("pathlib").Path.home() / ".aar" / "config.json"


# ---------------------------------------------------------------------------
# ACP data models
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
        """Concatenated plain-text content across all parts."""
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
    """ACP agent manifest — describes the agent's identity and capabilities."""

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
# Internal run record (not serialised over the wire)
# ---------------------------------------------------------------------------


class _RunRecord:
    """Mutable in-process state for a single run."""

    def __init__(self, run: AcpRun) -> None:
        self.run = run
        self.cancel_event: asyncio.Event = asyncio.Event()
        self.task: asyncio.Task | None = None
        # ACP event log (append-only; used by GET /runs/{id}/events)
        self.acp_events: list[AcpSseEvent] = []


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _auto_approve(spec: ToolSpec, tc: ToolCall) -> ApprovalResult:
    logger.info("ACP transport: auto-approving %s", tc.tool_name)
    return ApprovalResult.APPROVED


def _sse_line(obj: BaseModel) -> bytes:
    """Encode a Pydantic model as a single SSE data line."""
    return f"data: {obj.model_dump_json()}\n\n".encode()


# ---------------------------------------------------------------------------
# AcpTransport
# ---------------------------------------------------------------------------


class AcpTransport:
    """Bridges Aar's agent runtime to the ACP REST/SSE protocol."""

    def __init__(
        self,
        config: AgentConfig | None = None,
        approval_callback: ApprovalCallback | None = None,
        registry: ToolRegistry | None = None,
        agent_name: str = "aar",
        agent_description: str = "Aar adaptive action & reasoning agent",
    ) -> None:
        if config is None:
            from pathlib import Path

            p = Path.home() / ".aar" / "config.json"
            config = load_config(p) if p.is_file() else AgentConfig()
        self.config = config
        self.approval_callback: ApprovalCallback = approval_callback or _auto_approve
        self.registry = registry
        self.agent_name = agent_name
        self.agent_description = agent_description
        self.store = SessionStore(config.session_dir)
        self._runs: dict[str, _RunRecord] = {}

    # ------------------------------------------------------------------
    # Agent manifest
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Run creation
    # ------------------------------------------------------------------

    async def create_run(
        self,
        agent_name: str,
        input_messages: list[AcpMessage],
        mode: RunMode,
        session_id: str | None = None,
    ) -> tuple[AcpRun, asyncio.Queue[AcpSseEvent | None] | None]:
        """Create and start a new run.

        Returns (run, queue).  For stream mode *queue* carries SSE events;
        for sync/async it is ``None``.  Sync mode blocks until completion.
        """
        if agent_name != self.agent_name:
            raise ValueError(f"Unknown agent: {agent_name!r}")

        # Extract plain-text prompt from ACP messages
        prompt = "\n".join(m.text for m in input_messages if m.role == "user") or ""

        run = AcpRun(
            agent_name=agent_name,
            status=RunStatus.CREATED,
            session_id=session_id,
        )
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

        # STREAM — caller consumes the queue as SSE
        queue: asyncio.Queue[AcpSseEvent | None] = asyncio.Queue()
        run.status = RunStatus.IN_PROGRESS
        record.task = asyncio.create_task(
            self._execute_run(record, prompt, session_id, queue=queue)
        )
        return run, queue

    # ------------------------------------------------------------------
    # Run execution (shared by all modes)
    # ------------------------------------------------------------------

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
            agent = self._make_agent()

            # Buffer for streaming: accumulate chunks into assistant messages
            _stream_buf: list[str] = []

            def _flush_stream_buf_sync() -> None:
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
                        # Flush any accumulated stream chunks first, then emit the full message
                        _flush_stream_buf_sync()
                        msg = AcpMessage.from_text("assistant", event.content)
                        evt = MessageCreatedEvent(message=msg)
                        record.acp_events.append(evt)
                        queue.put_nowait(evt)
                    else:
                        # Sync / async: collect output
                        run.output.append(AcpMessage.from_text("assistant", event.content))

            agent.on_event(on_event)

            # Load or create session
            session: Session | None = None
            if session_id:
                try:
                    session = self.store.load(session_id)
                except FileNotFoundError:
                    pass

            finished_session = await agent.run(
                prompt, session, cancel_event=record.cancel_event
            )
            self.store.save(finished_session)
            run.session_id = finished_session.session_id

            # Flush any remaining stream buffer
            if queue and _stream_buf:
                _flush_stream_buf_sync()

            # For sync/async collect final output if not already streamed
            if not queue:
                run.output = _collect_output(finished_session)

            if finished_session.state == AgentState.CANCELLED:
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
            logger.exception("ACP run %s failed", run.run_id)
            run.finish(RunStatus.FAILED, error=str(exc))
            evt = RunFailedEvent(run=run.model_copy())
            record.acp_events.append(evt)
            if queue:
                await queue.put(evt)

        finally:
            if queue:
                await queue.put(None)  # sentinel — end of stream

    # ------------------------------------------------------------------
    # Run queries & actions
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_agent(self) -> Agent:
        return Agent(
            config=self.config,
            approval_callback=self.approval_callback,
            registry=self.registry,
        )


# ---------------------------------------------------------------------------
# Output collector (sync / async modes)
# ---------------------------------------------------------------------------


def _collect_output(session: Session) -> list[AcpMessage]:
    """Extract final assistant messages from a completed session."""
    messages: list[AcpMessage] = []
    for event in session.events:
        if isinstance(event, AssistantMessage) and event.content:
            messages.append(AcpMessage.from_text("assistant", event.content))
    return messages


# ---------------------------------------------------------------------------
# Minimal ASGI application
# ---------------------------------------------------------------------------


def create_acp_asgi_app(
    config: AgentConfig | None = None,
    approval_callback: ApprovalCallback | None = None,
    registry: ToolRegistry | None = None,
    agent_name: str = "aar",
    agent_description: str = "Aar adaptive action & reasoning agent",
) -> Any:
    """Create a minimal ASGI app that speaks the ACP protocol.

    Endpoints (ACP v0.2)
    --------------------
    GET  /agents                  — list agents
    GET  /agents/{name}           — agent manifest
    POST /runs                    — create run (sync|async|stream)
    GET  /runs/{run_id}           — run status
    POST /runs/{run_id}           — resume run (reserved for awaiting)
    POST /runs/{run_id}/cancel    — cancel run
    GET  /runs/{run_id}/events    — ACP event log
    GET  /sessions/{session_id}   — session metadata
    GET  /ping                    — health check

    Args:
        config: Agent configuration. Defaults to ``~/.aar/config.json`` or
            built-in defaults when ``None``.
        approval_callback: Tool approval gate. Defaults to auto-approve.
        registry: Shared :class:`ToolRegistry`. If ``None``, each agent
            request builds a fresh registry from built-ins.
        agent_name: Name exposed in the ACP agent manifest.
        agent_description: Human-readable description in the manifest.
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

        # --- GET /ping ---
        if method == "GET" and path == "/ping":
            await _json(send, {"status": "ok"})

        # --- GET /agents ---
        elif method == "GET" and path == "/agents":
            manifest = transport.get_manifest()
            await _json(send, {"agents": [manifest.model_dump()]})

        # --- GET /agents/{name} ---
        elif method == "GET" and path.startswith("/agents/"):
            name = path[len("/agents/"):]
            if name == transport.agent_name:
                await _json(send, transport.get_manifest().model_dump())
            else:
                await _json(send, {"detail": f"Agent '{name}' not found"}, status=404)

        # --- POST /runs ---
        elif method == "POST" and path == "/runs":
            body = await _read_body(receive)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                await _json(send, {"detail": "Invalid JSON"}, status=400)
                return

            agent_name_req: str = data.get("agent_name", transport.agent_name)
            raw_input: list[dict] = data.get("input", [])
            mode_str: str = data.get("mode", "sync")
            session_id: str | None = data.get("session_id")

            try:
                mode = RunMode(mode_str)
            except ValueError:
                await _json(send, {"detail": f"Invalid mode: {mode_str!r}"}, status=400)
                return

            try:
                input_messages = [AcpMessage.model_validate(m) for m in raw_input]
            except Exception as exc:
                await _json(send, {"detail": f"Invalid input messages: {exc}"}, status=400)
                return

            try:
                run, queue = await transport.create_run(
                    agent_name=agent_name_req,
                    input_messages=input_messages,
                    mode=mode,
                    session_id=session_id,
                )
            except ValueError as exc:
                await _json(send, {"detail": str(exc)}, status=404)
                return

            if mode == RunMode.STREAM and queue is not None:
                await _sse_run_stream(send, queue)
            elif mode == RunMode.ASYNC:
                await _json(send, run.model_dump(), status=202)
            else:
                # SYNC — run is already complete
                await _json(send, run.model_dump())

        # --- GET /runs/{run_id} ---
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

        # --- POST /runs/{run_id}/cancel ---
        elif method == "POST" and path.endswith("/cancel") and "/runs/" in path:
            run_id = path[len("/runs/"):].removesuffix("/cancel")
            run = await transport.cancel_run(run_id)
            if run:
                await _json(send, run.model_dump())
            else:
                await _json(send, {"detail": f"Run '{run_id}' not found"}, status=404)

        # --- GET /runs/{run_id}/events ---
        elif method == "GET" and path.endswith("/events") and "/runs/" in path:
            run_id = path[len("/runs/"):].removesuffix("/events")
            events = transport.get_run_events(run_id)
            if events is not None:
                await _json(send, {"events": [e.model_dump() for e in events]})
            else:
                await _json(send, {"detail": f"Run '{run_id}' not found"}, status=404)

        # --- POST /runs/{run_id} (resume) ---
        elif method == "POST" and _matches(path, "/runs/", 1):
            run_id = _path_tail(path, "/runs/")
            run = transport.get_run(run_id)
            if run:
                # Resuming awaiting runs is a future extension; ack for now.
                await _json(
                    send,
                    {"detail": "Resume not yet supported; run is not in awaiting state"},
                    status=422,
                )
            else:
                await _json(send, {"detail": f"Run '{run_id}' not found"}, status=404)

        # --- GET /sessions/{session_id} ---
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
# ASGI helpers (mirrors web.py style)
# ---------------------------------------------------------------------------


def _matches(path: str, prefix: str, min_segments: int) -> bool:
    """Return True when *path* starts with *prefix* and has enough segments."""
    if not path.startswith(prefix):
        return False
    tail = path[len(prefix):]
    return len(tail.split("/")) >= min_segments


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
    """Stream ACP SSE events from *queue* until sentinel (None) is received."""
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
            await send(
                {
                    "type": "http.response.body",
                    "body": _sse_line(event),
                    "more_body": True,
                }
            )
    finally:
        await send({"type": "http.response.body", "body": b"", "more_body": False})
