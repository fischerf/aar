"""Web transport — lightweight HTTP/SSE server for the agent.

Provides a REST API and Server-Sent Events stream so any web frontend
can interact with the agent over HTTP. No heavy framework required —
uses only the standard library + httpx for consistency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from agent.core.agent import Agent
from agent.core.config import AgentConfig, load_config
from agent.core.events import Event, ToolCall
from agent.core.session import Session
from agent.memory.session_store import SessionStore
from agent.safety.permissions import ApprovalCallback, ApprovalResult
from agent.tools.registry import ToolRegistry
from agent.tools.schema import ToolSpec
from agent.transports.stream import EventStream

logger = logging.getLogger(__name__)

_USER_CONFIG = Path.home() / ".aar" / "config.json"


async def _auto_approve_callback(spec: ToolSpec, tc: ToolCall) -> ApprovalResult:
    """Default web approval: auto-approve all tool calls.

    In the web transport there is no interactive terminal, so the act of
    sending a request to the API is treated as implicit approval.  Inject a
    custom *approval_callback* into :class:`WebTransport` when you need
    stricter control (e.g. an async webhook).
    """
    logger.info("Web transport: auto-approving %s", tc.tool_name)
    return ApprovalResult.APPROVED


class WebTransport:
    """Manages agent sessions and exposes them over an event-stream interface.

    This class is framework-agnostic — it produces dicts and event streams
    that any HTTP framework (FastAPI, Starlette, aiohttp, etc.) can serve.
    """

    def __init__(
        self,
        config: AgentConfig | None = None,
        approval_callback: ApprovalCallback | None = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        if config is None:
            if _USER_CONFIG.is_file():
                config = load_config(_USER_CONFIG)
            else:
                config = AgentConfig()
        self.config = config
        self.approval_callback: ApprovalCallback = (
            approval_callback if approval_callback is not None else _auto_approve_callback
        )
        self.registry = registry  # shared across requests; None = each Agent builds its own
        self.store = SessionStore(self.config.session_dir)
        self._active_streams: dict[str, EventStream] = {}
        self._sessions: dict[str, Session] = {}

    def _make_agent(self, safety_override: dict | None = None) -> Agent:
        if safety_override:
            merged_safety = self.config.safety.model_copy(update=safety_override)
            config = self.config.model_copy(update={"safety": merged_safety})
        else:
            config = self.config
        return Agent(
            config=config,
            approval_callback=self.approval_callback,
            registry=self.registry,
        )

    async def handle_chat(
        self, prompt: str, session_id: str | None = None, safety_override: dict | None = None
    ) -> dict[str, Any]:
        """Handle a chat request. Returns the response payload.

        If session_id is provided, continues that session.
        If safety_override is provided, those SafetyConfig fields override the server defaults
        for this request only.
        """
        agent = self._make_agent(safety_override)

        # Set up event stream for this request
        collected_events: list[dict[str, Any]] = []

        def collect(event: Event) -> None:
            collected_events.append(event.model_dump())
            # Also push to SSE stream if active
            req_stream = self._active_streams.get(session_id or "")
            if req_stream:
                req_stream.emit(event)

        agent.on_event(collect)

        # Load or create session
        session: Session | None = None
        if session_id:
            try:
                session = self.store.load(session_id)
                self._sessions[session_id] = session
            except FileNotFoundError:
                pass

        session = await agent.run(prompt, session)
        self.store.save(session)
        self._sessions[session.session_id] = session

        # Emit a terminal event so the events list has a clear "done" marker.
        from agent.core.events import AssistantMessage, SessionEvent
        from agent.core.events import ToolResult as ToolResultEvent

        ended_event = SessionEvent(action="ended", data={"state": session.state.value})
        collect(ended_event)

        # Collect the final assistant text and all tool results in one forward pass.
        # Iterating forward and overwriting means the LAST non-empty assistant text wins.
        final_text = ""
        tool_results: list[dict[str, Any]] = []
        for event in session.events:
            if isinstance(event, AssistantMessage) and event.content:
                final_text = event.content
            elif isinstance(event, ToolResultEvent):
                tool_results.append(
                    {
                        "tool_name": event.tool_name,
                        "output": event.output,
                        "is_error": event.is_error,
                        "duration_ms": event.duration_ms,
                    }
                )

        # When the model completes via tools without producing any narrating text
        # (common for tool-heavy tasks), fall back to the last successful tool
        # output so the caller always gets something meaningful in `response`.
        if not final_text and tool_results:
            last_ok = next((r for r in reversed(tool_results) if not r["is_error"]), None)
            if last_ok:
                final_text = last_ok["output"]

        return {
            "session_id": session.session_id,
            "response": final_text,
            "tool_results": tool_results,
            "events": collected_events,
            "state": session.state.value,
            "step_count": session.step_count,
        }

    async def handle_stream(
        self, prompt: str, session_id: str | None = None, safety_override: dict | None = None
    ) -> AsyncEventIterator:
        """Handle a streaming chat request. Returns an async iterator of SSE events.

        If safety_override is provided, those SafetyConfig fields override the server defaults
        for this request only.
        """
        stream = EventStream()
        queue: asyncio.Queue[Event | None] = asyncio.Queue()

        eff_session_id = session_id or uuid.uuid4().hex[:16]
        self._active_streams[eff_session_id] = stream

        def on_event(event: Event) -> None:
            queue.put_nowait(event)

        stream.subscribe(on_event)

        async def run_agent() -> None:
            try:
                agent = self._make_agent(safety_override)
                agent.on_event(on_event)
                session = None
                if session_id:
                    try:
                        session = self.store.load(session_id)
                    except FileNotFoundError:
                        pass
                session = await agent.run(prompt, session)
                self.store.save(session)
                # Emit a terminal event BEFORE closing the queue so SSE clients
                # receive an explicit "done" signal rather than relying on
                # stream-close detection.
                from agent.core.events import SessionEvent

                on_event(
                    SessionEvent(
                        action="ended",
                        data={
                            "state": session.state.value,
                            "step_count": session.step_count,
                        },
                    )
                )
            finally:
                queue.put_nowait(None)  # Signal end of async iterator
                self._active_streams.pop(eff_session_id, None)

        # Start the agent in the background
        task = asyncio.create_task(run_agent())

        return AsyncEventIterator(queue, task, eff_session_id)

    def list_sessions(self) -> list[str]:
        return self.store.list_sessions()

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


class AsyncEventIterator:
    """Async iterator that yields SSE-formatted event strings."""

    def __init__(
        self,
        queue: asyncio.Queue[Event | None],
        task: asyncio.Task,
        session_id: str,
    ) -> None:
        self._queue = queue
        self._task = task
        self.session_id = session_id

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        event = await self._queue.get()
        if event is None:
            raise StopAsyncIteration
        return format_sse(event)

    async def cancel(self) -> None:
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass


def format_sse(event: Event) -> str:
    """Format an event as a Server-Sent Events message."""
    data = event.model_dump_json()
    return f"event: {event.type.value}\ndata: {data}\n\n"


# --- Optional: minimal ASGI app for quick deployment ---


def create_asgi_app(
    config: AgentConfig | None = None,
    approval_callback: ApprovalCallback | None = None,
    registry: ToolRegistry | None = None,
) -> Any:
    """Create a minimal ASGI application wrapping the web transport.

    Requires no external framework — uses raw ASGI protocol.
    Endpoints:
        POST /chat          — JSON body {prompt, session_id?} → JSON response
        POST /chat/stream   — JSON body {prompt, session_id?} → SSE stream
        GET  /sessions      — list session IDs
        GET  /sessions/{id} — session details
        GET  /health        — health check

    Args:
        config: Agent configuration. If None, auto-loads ``~/.aar/config.json``
            or falls back to built-in defaults.
        approval_callback: Called when a tool needs human approval. Defaults to
            ``_auto_approve_callback`` (auto-approve all — the HTTP request is
            treated as implicit approval). Pass a custom callback for webhook-
            style approval or to deny all writes.
        registry: Optional shared :class:`ToolRegistry`. Use this to expose MCP
            tools over the web API (register them once, reuse across requests).
            If None, each agent request builds a fresh registry from built-ins.
    """
    transport = WebTransport(config, approval_callback, registry)

    async def app(scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return

        path = scope["path"]
        method = scope["method"]

        if method == "OPTIONS":
            await _cors_preflight(send)
            return

        if method == "GET" and path == "/health":
            await _json_response(send, {"status": "ok"})

        elif method == "GET" and path == "/sessions":
            sessions = transport.list_sessions()
            await _json_response(send, {"sessions": sessions})

        elif method == "GET" and path.startswith("/sessions/"):
            sid = path.split("/sessions/", 1)[1]
            info = transport.get_session(sid)
            if info:
                await _json_response(send, info)
            else:
                await _json_response(send, {"error": "not found"}, status=404)

        elif method == "POST" and path == "/chat":
            body = await _read_body(receive)
            data = json.loads(body)
            result = await transport.handle_chat(
                prompt=data["prompt"],
                session_id=data.get("session_id"),
                safety_override=data.get("safety"),
            )
            await _json_response(send, result)

        elif method == "POST" and path == "/chat/stream":
            body = await _read_body(receive)
            data = json.loads(body)
            iterator = await transport.handle_stream(
                prompt=data["prompt"],
                session_id=data.get("session_id"),
                safety_override=data.get("safety"),
            )
            await _sse_response(send, iterator)

        else:
            await _json_response(send, {"error": "not found"}, status=404)

    return app


async def _read_body(receive: Any) -> bytes:
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if not msg.get("more_body", False):
            break
    return body


_CORS_HEADERS = [
    [b"access-control-allow-origin", b"*"],
    [b"access-control-allow-methods", b"GET, POST, OPTIONS"],
    [b"access-control-allow-headers", b"content-type"],
]


async def _cors_preflight(send: Any) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 204,
            "headers": _CORS_HEADERS,
        }
    )
    await send({"type": "http.response.body", "body": b""})


async def _json_response(send: Any, data: dict, status: int = 200) -> None:
    body = json.dumps(data).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(body)).encode()],
                *_CORS_HEADERS,
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _sse_response(send: Any, iterator: AsyncEventIterator) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                [b"content-type", b"text/event-stream"],
                [b"cache-control", b"no-cache"],
                [b"connection", b"keep-alive"],
                *_CORS_HEADERS,
            ],
        }
    )
    try:
        async for chunk in iterator:
            await send(
                {
                    "type": "http.response.body",
                    "body": chunk.encode(),
                    "more_body": True,
                }
            )
    finally:
        await send({"type": "http.response.body", "body": b"", "more_body": False})
