"""Extension UI panels over the ACP HTTP/SSE transport.

* one cached ``Agent`` per session — extension state survives across runs
* ``GET /sessions/{id}/panels``, ``GET /sessions/{id}/panels/{name}``,
  ``POST /sessions/{id}/panels/{name}/actions/{action}``
* error mapping (404 / 400 / 422 / 409)
* ``panel_changed`` SSE event in stream mode

The fake panel from ``tests.test_extension_panels`` is injected into the
session's cached agent so no real extension has to be installed.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agent.core.agent import Agent
from agent.transports.acp.http import (
    AcpRun,
    PanelChangedEvent,
    RunStatus,
    _RunRecord,
    create_acp_asgi_app,
)
from tests.conftest import MockProvider
from tests.test_acp import _auth_headers, _call_asgi, _make_config
from tests.test_extension_panels import make_manager, make_panel


def _make_app(provider: MockProvider) -> Any:
    app = create_acp_asgi_app(config=_make_config(), agent_name="test-agent")
    transport = app.transport
    made: list[Agent] = []

    def patched_make() -> Agent:
        agent = Agent(
            config=transport.config,
            provider=provider,
            approval_callback=transport.approval_callback,
            registry=transport.registry,
        )
        made.append(agent)
        return agent

    transport._make_agent = patched_make  # type: ignore[method-assign]
    app._made_agents = made  # type: ignore[attr-defined]
    return app


def _run_body(text: str, session_id: str | None = None, mode: str = "sync") -> dict[str, Any]:
    body: dict[str, Any] = {
        "agent_name": "test-agent",
        "mode": mode,
        "input": [{"role": "user", "parts": [{"content_type": "text/plain", "content": text}]}],
    }
    if session_id:
        body["session_id"] = session_id
    return body


async def _start_session(app: Any, provider: MockProvider) -> str:
    provider.enqueue_text("hello", stop="end_turn")
    status, run = await _call_asgi(app, "POST", "/runs", _run_body("hi"))
    assert status == 200, run
    return run["session_id"]


async def _sse(app: Any, body: dict[str, Any]) -> list[dict[str, Any]]:
    """Drive a stream-mode run and return the decoded SSE events."""
    payload = json.dumps(body).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/runs",
        "query_string": b"",
        "headers": _auth_headers(app),
    }
    chunks: list[bytes] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    events = []
    for line in b"".join(chunks).decode().split("\n\n"):
        line = line.strip()
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: ") :]))
    return events


class TestAgentPerSession:
    @pytest.mark.asyncio
    async def test_same_session_reuses_agent(self):
        provider = MockProvider()
        app = _make_app(provider)
        sid = await _start_session(app, provider)

        provider.enqueue_text("again", stop="end_turn")
        status, run2 = await _call_asgi(app, "POST", "/runs", _run_body("more", sid))
        assert status == 200 and run2["session_id"] == sid

        assert len(app._made_agents) == 1
        assert app.transport._agents[sid] is app._made_agents[0]
        # The per-run listener was removed again.
        assert app._made_agents[0]._on_event == []

    @pytest.mark.asyncio
    async def test_different_sessions_get_different_agents(self):
        provider = MockProvider()
        app = _make_app(provider)
        sid1 = await _start_session(app, provider)
        sid2 = await _start_session(app, provider)
        assert sid1 != sid2
        assert len(app._made_agents) == 2


class TestPanelEndpoints:
    @pytest.mark.asyncio
    async def test_list_snapshot_action_roundtrip(self):
        provider = MockProvider()
        app = _make_app(provider)
        sid = await _start_session(app, provider)

        calls: list[tuple[str, str, dict[str, Any]]] = []
        panel, state = make_panel(calls)
        app.transport._agents[sid]._extension_manager = make_manager(panel)

        status, listed = await _call_asgi(app, "GET", f"/sessions/{sid}/panels")
        assert status == 200, listed
        assert listed["session_id"] == sid
        assert [p["name"] for p in listed["panels"]] == ["demo"]
        assert listed["panels"][0]["status"] == "demo 2"
        assert [a["id"] for a in listed["panels"][0]["actions"]] == ["undo", "refresh"]

        status, snap = await _call_asgi(app, "GET", f"/sessions/{sid}/panels/demo")
        assert status == 200
        assert snap["root"]["id"] == "root"
        assert [c["id"] for c in snap["root"]["children"][0]["children"]] == ["cp:2", "cp:1"]

        status, acted = await _call_asgi(
            app,
            "POST",
            f"/sessions/{sid}/panels/demo/actions/undo",
            {"node_id": "cp:2", "args": {"force": True}},
        )
        assert status == 200, acted
        assert acted["message"] == "undone cp:2"
        assert calls == [("undo", "cp:2", {"force": True})]
        assert state["n"] == 1
        assert [c["id"] for c in acted["root"]["children"][0]["children"]] == ["cp:1"]
        assert acted["status"] == "demo 1"

        # camelCase node id and an empty body are accepted for non-mutating actions
        status, acted2 = await _call_asgi(
            app, "POST", f"/sessions/{sid}/panels/demo/actions/refresh", {"nodeId": "root"}
        )
        assert status == 200 and acted2["message"] is None
        assert calls[-1][0] == "refresh"

    @pytest.mark.asyncio
    async def test_error_mapping(self):
        provider = MockProvider()
        app = _make_app(provider)
        sid = await _start_session(app, provider)
        panel, _ = make_panel([])
        app.transport._agents[sid]._extension_manager = make_manager(panel)

        status, _ = await _call_asgi(app, "GET", "/sessions/nope/panels")
        assert status == 404
        status, _ = await _call_asgi(app, "GET", f"/sessions/{sid}/panels/ghost")
        assert status == 404
        status, _ = await _call_asgi(
            app, "POST", f"/sessions/{sid}/panels/demo/actions/explode", {"node_id": "root"}
        )
        assert status == 404
        status, _ = await _call_asgi(
            app, "POST", f"/sessions/{sid}/panels/demo/actions/undo", {"node_id": "missing"}
        )
        assert status == 404
        status, _ = await _call_asgi(app, "POST", f"/sessions/{sid}/panels/demo/actions/undo", {})
        assert status == 400
        status, _ = await _call_asgi(
            app, "POST", f"/sessions/{sid}/panels/demo/actions/undo", {"node_id": "root"}
        )
        assert status == 422  # undo applies to checkpoints only
        status, _ = await _call_asgi(app, "GET", f"/sessions/{sid}/panels/demo/extra/junk")
        assert status == 404

    @pytest.mark.asyncio
    async def test_mutating_action_conflicts_with_running_run(self):
        provider = MockProvider()
        app = _make_app(provider)
        sid = await _start_session(app, provider)
        calls: list[tuple[str, str, dict[str, Any]]] = []
        panel, _ = make_panel(calls)
        app.transport._agents[sid]._extension_manager = make_manager(panel)

        busy = AcpRun(agent_name="test-agent", status=RunStatus.IN_PROGRESS, session_id=sid)
        app.transport._runs[busy.run_id] = _RunRecord(busy)
        try:
            status, body = await _call_asgi(
                app, "POST", f"/sessions/{sid}/panels/demo/actions/undo", {"node_id": "cp:2"}
            )
            assert status == 409, body
            assert calls == []
            # non-mutating actions still work
            status, _ = await _call_asgi(
                app, "POST", f"/sessions/{sid}/panels/demo/actions/refresh", {"node_id": "root"}
            )
            assert status == 200
        finally:
            app.transport._runs.pop(busy.run_id, None)

    @pytest.mark.asyncio
    async def test_panels_require_auth(self):
        provider = MockProvider()
        app = _make_app(provider)
        sid = await _start_session(app, provider)
        status, body = await _call_asgi(app, "GET", f"/sessions/{sid}/panels", headers=[])
        assert status == 401


class TestPanelChangedSse:
    @pytest.mark.asyncio
    async def test_stream_emits_panel_changed(self):
        provider = MockProvider()
        app = _make_app(provider)
        sid = await _start_session(app, provider)
        panel, _ = make_panel([])
        app.transport._agents[sid]._extension_manager = make_manager(panel)
        panel.changed.set()

        provider.enqueue_text("streamed", stop="end_turn")
        events = await _sse(app, _run_body("go", sid, mode="stream"))
        types = [e["type"] for e in events]
        assert types[0] == "run_in_progress"
        assert "panel_changed" in types
        assert types.index("panel_changed") < types.index("run_completed")
        changed = next(e for e in events if e["type"] == "panel_changed")
        assert changed["session_id"] == sid and changed["panel"] == "demo"
        assert not panel.changed.is_set()

        # And it is in the persisted event log too.
        run_id = changed["run_id"]
        status, log = await _call_asgi(app, "GET", f"/runs/{run_id}/events")
        assert status == 200
        assert any(e["type"] == "panel_changed" for e in log["events"])

    @pytest.mark.asyncio
    async def test_sync_run_without_change_emits_nothing(self):
        provider = MockProvider()
        app = _make_app(provider)
        sid = await _start_session(app, provider)
        panel, _ = make_panel([])
        app.transport._agents[sid]._extension_manager = make_manager(panel)
        panel.changed.clear()

        provider.enqueue_text("quiet", stop="end_turn")
        status, run = await _call_asgi(app, "POST", "/runs", _run_body("x", sid))
        assert status == 200
        status, log = await _call_asgi(app, "GET", f"/runs/{run['run_id']}/events")
        assert not any(isinstance(e, PanelChangedEvent) for e in log["events"])
        assert not any(e["type"] == "panel_changed" for e in log["events"])
        await asyncio.sleep(0)
