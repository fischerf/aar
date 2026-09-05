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
from agent.memory.session_store import SessionStore, validate_session_id
from agent.safety.permissions import ApprovalCallback, ApprovalResult
from agent.tools.registry import ToolRegistry
from agent.tools.schema import ToolSpec
from agent.transports._http_auth import (
    PUBLIC_PATHS,
    BearerAuth,
    apply_safety_override,
    cors_headers,
    normalize_origins,
)
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


async def _deny_approval_callback(spec: ToolSpec, tc: ToolCall) -> ApprovalResult:
    """Default web approval: refuse anything the policy wants a human to see.

    C1 — ``_auto_approve_callback`` combined with an unauthenticated endpoint
    meant a request could run ``bash`` with no human in the loop.  Denying by
    default keeps read-only work flowing while requiring an explicit
    ``--approval auto`` (or a custom callback) to hand over write/execute.
    """
    logger.warning(
        "Web transport: denying %s (no approval channel; pass --approval auto to allow)",
        tc.tool_name,
    )
    return ApprovalResult.DENIED


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
        allow_safety_override: bool = False,
    ) -> None:
        if config is None:
            if _USER_CONFIG.is_file():
                config = load_config(_USER_CONFIG)
            else:
                config = AgentConfig()
        self.config = config
        # C1 — Deny by default. The web transport has no interactive channel,
        # so "the request itself is implicit approval" meant an unauthenticated
        # POST could run arbitrary shell commands.
        self.approval_callback: ApprovalCallback = (
            approval_callback if approval_callback is not None else _deny_approval_callback
        )
        self.registry = registry  # shared across requests; None = each Agent builds its own
        # C1 — When False (the default) a client-supplied ``safety`` block may
        # only tighten the server policy, never loosen it.
        self.allow_safety_override = allow_safety_override
        self.store = SessionStore(self.config.session_dir)
        self._active_streams: dict[str, EventStream] = {}
        self._sessions: dict[str, Session] = {}

    def _make_agent(
        self,
        safety_override: dict | None = None,
        provider_override: str | None = None,
    ) -> Agent:
        config = self.config
        if safety_override:
            merged_safety = apply_safety_override(
                config.safety, safety_override, self.allow_safety_override
            )
            if merged_safety is not config.safety:
                config = config.model_copy(update={"safety": merged_safety})
        if provider_override:
            try:
                provider_cfg = config.resolve_provider(provider_override)
                config = config.model_copy(update={"provider": provider_cfg})
            except ValueError:
                pass  # fall through to default
        return Agent(
            config=config,
            approval_callback=self.approval_callback,
            registry=self.registry,
        )

    async def handle_chat(
        self,
        prompt: str,
        session_id: str | None = None,
        safety_override: dict | None = None,
        provider_override: str | None = None,
    ) -> dict[str, Any]:
        """Handle a chat request. Returns the response payload.

        If session_id is provided, continues that session.
        If safety_override is provided, those SafetyConfig fields override the server defaults
        for this request only.
        If provider_override is provided, use that named provider key for this request.
        """
        # #7 — Always allocate a concrete session id up front so the
        # ``_active_streams`` lookup is keyed deterministically. Previously
        # both ``handle_chat`` and ``handle_stream`` keyed on
        # ``session_id or ""``, which caused concurrent no-session requests
        # to bleed events into each other (or, after the UUID was added on the
        # stream side, to never match at all).
        eff_session_id = session_id or uuid.uuid4().hex[:16]

        agent = self._make_agent(safety_override, provider_override)

        # Set up event stream for this request
        collected_events: list[dict[str, Any]] = []

        def collect(event: Event) -> None:
            collected_events.append(event.model_dump())
            # Also push to SSE stream if active
            req_stream = self._active_streams.get(eff_session_id)
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
        self,
        prompt: str,
        session_id: str | None = None,
        safety_override: dict | None = None,
        provider_override: str | None = None,
    ) -> AsyncEventIterator:
        """Handle a streaming chat request. Returns an async iterator of SSE events.

        If safety_override is provided, those SafetyConfig fields override the server defaults
        for this request only.
        If provider_override is provided, use that named provider key for this request.
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
                agent = self._make_agent(safety_override, provider_override)
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
    *,
    auth: BearerAuth | None = None,
    cors_origins: list[str] | None = None,
    allow_safety_override: bool = False,
) -> Any:
    """Create a minimal ASGI application wrapping the web transport.

    Requires no external framework — uses raw ASGI protocol.
    Endpoints:
        POST /chat          — JSON body {prompt, session_id?} → JSON response
        POST /chat/stream   — JSON body {prompt, session_id?} → SSE stream
        GET  /sessions      — list session IDs
        GET  /sessions/{id} — session details
        GET  /health        — health check (the only unauthenticated route)

    Args:
        config: Agent configuration. If None, auto-loads ``~/.aar/config.json``
            or falls back to built-in defaults.
        approval_callback: Called when a tool needs human approval. Defaults to
            ``_deny_approval_callback`` — anything the policy wants a human to
            confirm is refused. Pass ``_auto_approve_callback`` (or your own)
            to opt into unattended execution.
        registry: Optional shared :class:`ToolRegistry`. Use this to expose MCP
            tools over the web API (register them once, reuse across requests).
            If None, each agent request builds a fresh registry from built-ins.
        auth: Bearer-token gate. Defaults to a fresh :class:`BearerAuth` with a
            generated token — read ``app.auth.token`` to learn it, or pass
            ``BearerAuth.disabled()`` when fronted by your own auth layer.
        cors_origins: Exact origins allowed to make cross-origin requests.
            Empty (the default) emits no CORS headers at all.
        allow_safety_override: Let a request body replace the server's safety
            policy wholesale instead of only tightening it.
    """
    transport = WebTransport(
        config,
        approval_callback if approval_callback is not None else _deny_approval_callback,
        registry,
        allow_safety_override=allow_safety_override,
    )
    auth = auth if auth is not None else BearerAuth()
    allowed_origins = normalize_origins(cors_origins)

    async def app(scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return

        path = scope["path"]
        method = scope["method"]
        cors = cors_headers(scope, allowed_origins)

        if method == "OPTIONS":
            await _cors_preflight(send, cors)
            return

        # C1 — Authenticate before routing. ``/health`` stays open so process
        # supervisors can probe liveness without holding the token.
        if path not in PUBLIC_PATHS and not auth.check(scope):
            await _json_response(send, {"error": "unauthorized"}, status=401, cors=cors)
            return

        if method == "GET" and path == "/health":
            await _json_response(send, {"status": "ok"}, cors=cors)

        elif method == "GET" and path == "/sessions":
            sessions = transport.list_sessions()
            await _json_response(send, {"sessions": sessions}, cors=cors)

        elif method == "GET" and path.startswith("/sessions/"):
            sid = path.split("/sessions/", 1)[1]
            info = transport.get_session(sid)
            if info:
                await _json_response(send, info, cors=cors)
            else:
                await _json_response(send, {"error": "not found"}, status=404, cors=cors)

        elif method == "POST" and path == "/chat":
            body = await _read_body(receive)
            try:
                data = json.loads(body)
                if not isinstance(data, dict) or "prompt" not in data:
                    raise ValueError("request body must be a JSON object with a 'prompt' field")
            except (ValueError, json.JSONDecodeError) as exc:
                # #7 — Malformed JSON used to bubble up as an uncaught
                # ``json.JSONDecodeError`` and surface as a 500. Treat it (and
                # missing required fields) as a 400 with a brief diagnostic.
                await _json_response(send, {"error": f"bad request: {exc}"}, status=400, cors=cors)
                return
            sid = data.get("session_id")
            if sid is not None:
                try:
                    validate_session_id(sid)
                except ValueError as exc:
                    await _json_response(
                        send, {"error": f"bad request: {exc}"}, status=400, cors=cors
                    )
                    return
            result = await transport.handle_chat(
                prompt=data["prompt"],
                session_id=sid,
                safety_override=data.get("safety"),
                provider_override=data.get("provider"),
            )
            await _json_response(send, result, cors=cors)

        elif method == "POST" and path == "/chat/stream":
            body = await _read_body(receive)
            try:
                data = json.loads(body)
                if not isinstance(data, dict) or "prompt" not in data:
                    raise ValueError("request body must be a JSON object with a 'prompt' field")
            except (ValueError, json.JSONDecodeError) as exc:
                await _json_response(send, {"error": f"bad request: {exc}"}, status=400, cors=cors)
                return
            sid = data.get("session_id")
            if sid is not None:
                try:
                    validate_session_id(sid)
                except ValueError as exc:
                    await _json_response(
                        send, {"error": f"bad request: {exc}"}, status=400, cors=cors
                    )
                    return
            iterator = await transport.handle_stream(
                prompt=data["prompt"],
                session_id=sid,
                safety_override=data.get("safety"),
                provider_override=data.get("provider"),
            )
            await _sse_response(send, receive, iterator, cors)

        else:
            await _json_response(send, {"error": "not found"}, status=404, cors=cors)

    app.auth = auth  # type: ignore[attr-defined]
    app.transport = transport  # type: ignore[attr-defined]
    return app


async def _read_body(receive: Any) -> bytes:
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if not msg.get("more_body", False):
            break
    return body


async def _cors_preflight(send: Any, cors: list[list[bytes]] | None = None) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 204,
            "headers": list(cors or []),
        }
    )
    await send({"type": "http.response.body", "body": b""})


async def _json_response(
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


async def _sse_response(
    send: Any,
    receive: Any,
    iterator: AsyncEventIterator,
    cors: list[list[bytes]] | None = None,
) -> None:
    # #7 — Monitor for ASGI ``http.disconnect`` in parallel with the SSE
    # write loop. If the client closes the connection we must cancel the
    # background run task; otherwise the agent keeps burning tokens until
    # natural completion even though no one is listening.
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

    disconnect_event = asyncio.Event()

    async def _watch_disconnect() -> None:
        try:
            while not disconnect_event.is_set():
                msg = await receive()
                if msg.get("type") == "http.disconnect":
                    disconnect_event.set()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            # Defensive: any ASGI receive error is treated as a disconnect
            # so the agent doesn't keep running forever on a wedged client.
            disconnect_event.set()

    watcher = asyncio.create_task(_watch_disconnect())
    try:
        iter_obj = iterator.__aiter__()
        while True:
            next_task = asyncio.ensure_future(iter_obj.__anext__())
            disconnect_task = asyncio.ensure_future(disconnect_event.wait())
            done, _pending = await asyncio.wait(
                {next_task, disconnect_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnect_task in done and next_task not in done:
                next_task.cancel()
                try:
                    await next_task
                except (asyncio.CancelledError, StopAsyncIteration, Exception):
                    pass
                logger.info("SSE client disconnected; cancelling agent run")
                await iterator.cancel()
                break
            # next_task completed first — send the chunk (or stop on end).
            disconnect_task.cancel()
            try:
                chunk = next_task.result()
            except StopAsyncIteration:
                break
            try:
                await send(
                    {
                        "type": "http.response.body",
                        "body": chunk.encode(),
                        "more_body": True,
                    }
                )
            except Exception:
                # Send failure usually means the peer is gone — cancel and exit.
                await iterator.cancel()
                break
    finally:
        watcher.cancel()
        try:
            await watcher
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except Exception:
            pass
