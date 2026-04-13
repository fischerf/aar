"""Tests for the ACP transport (Agent Communication Protocol v0.2)."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from agent.core.config import AgentConfig, ProviderConfig, SafetyConfig, ToolConfig
from agent.transports.acp import (
    AcpMessage,
    AcpRun,
    AcpTransport,
    AgentManifest,
    MessagePart,
    RunCancelledEvent,
    RunCompletedEvent,
    RunCreatedEvent,
    RunFailedEvent,
    RunInProgressEvent,
    RunMode,
    RunStatus,
    _collect_output,
    _sse_line,
    create_acp_asgi_app,
)
from tests.conftest import MockProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config() -> AgentConfig:
    return AgentConfig(
        provider=ProviderConfig(name="mock", model="mock-1"),
        max_steps=5,
        timeout=10.0,
        safety=SafetyConfig(
            require_approval_for_writes=False,
            require_approval_for_execute=False,
        ),
        tools=ToolConfig(enabled_builtins=[]),
    )


def _make_transport(provider: MockProvider) -> AcpTransport:
    config = _make_config()
    transport = AcpTransport(config=config, agent_name="test-agent", agent_description="Test")

    # Inject the mock provider directly — bypasses _create_provider's registry lookup
    def patched_make() -> "Agent":  # noqa: F821
        from agent.core.agent import Agent

        return Agent(
            config=transport.config,
            provider=provider,
            approval_callback=transport.approval_callback,
            registry=transport.registry,
        )

    transport._make_agent = patched_make  # type: ignore[method-assign]
    return transport


def _user_msg(text: str) -> AcpMessage:
    return AcpMessage.from_text("user", text)


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestAcpModels:
    def test_message_part_defaults(self):
        part = MessagePart()
        assert part.content_type == "text/plain"
        assert part.content == ""

    def test_acp_message_text_property(self):
        msg = AcpMessage(
            role="user",
            parts=[
                MessagePart(content_type="text/plain", content="hello "),
                MessagePart(content_type="text/plain", content="world"),
                MessagePart(content_type="image/png", content="<base64>"),
            ],
        )
        assert msg.text == "hello world"

    def test_acp_message_from_text(self):
        msg = AcpMessage.from_text("assistant", "hi there")
        assert msg.role == "assistant"
        assert len(msg.parts) == 1
        assert msg.parts[0].content == "hi there"
        assert msg.text == "hi there"

    def test_run_status_values(self):
        assert RunStatus.CREATED.value == "created"
        assert RunStatus.IN_PROGRESS.value == "in-progress"
        assert RunStatus.COMPLETED.value == "completed"
        assert RunStatus.FAILED.value == "failed"
        assert RunStatus.CANCELLED.value == "cancelled"

    def test_run_mode_values(self):
        assert RunMode.SYNC.value == "sync"
        assert RunMode.ASYNC.value == "async"
        assert RunMode.STREAM.value == "stream"

    def test_acp_run_finish(self):
        run = AcpRun(agent_name="aar")
        run.finish(RunStatus.COMPLETED)
        assert run.status == RunStatus.COMPLETED
        assert run.finished_at is not None
        assert run.error is None

    def test_acp_run_finish_with_error(self):
        run = AcpRun(agent_name="aar")
        run.finish(RunStatus.FAILED, error="boom")
        assert run.status == RunStatus.FAILED
        assert run.error == "boom"

    def test_agent_manifest_serialises(self):
        manifest = AgentManifest(
            name="aar",
            description="Aar agent",
            input_content_types=["text/plain"],
            output_content_types=["text/plain"],
        )
        data = manifest.model_dump()
        assert data["name"] == "aar"
        assert "text/plain" in data["input_content_types"]


class TestSseLine:
    def test_sse_line_format(self):
        run = AcpRun(agent_name="aar", status=RunStatus.CREATED)
        evt = RunCreatedEvent(run=run)
        line = _sse_line(evt)
        assert line.startswith(b"data: ")
        assert line.endswith(b"\n\n")
        payload = json.loads(line[len(b"data: ") :].decode())
        assert payload["type"] == "run_created"
        assert "run" in payload


# ---------------------------------------------------------------------------
# AcpTransport unit tests
# ---------------------------------------------------------------------------


class TestAcpTransportManifest:
    def test_manifest_name_and_description(self):
        config = _make_config()
        transport = AcpTransport(config=config, agent_name="my-agent", agent_description="Desc")
        manifest = transport.get_manifest()
        assert manifest.name == "my-agent"
        assert manifest.description == "Desc"
        assert "text/plain" in manifest.input_content_types
        assert manifest.metadata["provider"] == "mock"

    def test_manifest_reflects_config_model(self):
        config = _make_config()
        transport = AcpTransport(config=config, agent_name="aar")
        manifest = transport.get_manifest()
        assert manifest.metadata["model"] == "mock-1"


class TestAcpTransportSync:
    @pytest.mark.asyncio
    async def test_sync_run_completes(self):
        provider = MockProvider()
        provider.enqueue_text("Hello from Aar!", stop="end_turn")
        transport = _make_transport(provider)

        run, queue = await transport.create_run(
            agent_name="test-agent",
            input_messages=[_user_msg("Hi")],
            mode=RunMode.SYNC,
        )

        assert queue is None
        assert run.status == RunStatus.COMPLETED
        assert run.finished_at is not None
        assert any("Hello from Aar!" in m.text for m in run.output)

    @pytest.mark.asyncio
    async def test_sync_run_preserves_run_id(self):
        provider = MockProvider()
        provider.enqueue_text("ok", stop="end_turn")
        transport = _make_transport(provider)

        run, _ = await transport.create_run(
            agent_name="test-agent",
            input_messages=[_user_msg("test")],
            mode=RunMode.SYNC,
        )

        assert run.run_id in transport._runs
        assert transport.get_run(run.run_id) is run

    @pytest.mark.asyncio
    async def test_sync_run_unknown_agent_raises(self):
        provider = MockProvider()
        transport = _make_transport(provider)

        with pytest.raises(ValueError, match="Unknown agent"):
            await transport.create_run(
                agent_name="nonexistent",
                input_messages=[_user_msg("hi")],
                mode=RunMode.SYNC,
            )

    @pytest.mark.asyncio
    async def test_sync_run_event_log(self):
        provider = MockProvider()
        provider.enqueue_text("Done", stop="end_turn")
        transport = _make_transport(provider)

        run, _ = await transport.create_run(
            agent_name="test-agent",
            input_messages=[_user_msg("go")],
            mode=RunMode.SYNC,
        )

        events = transport.get_run_events(run.run_id)
        assert events is not None
        types = [e.type for e in events]
        assert "run_created" in types
        assert "run_in_progress" in types
        assert "run_completed" in types


class TestAcpTransportAsync:
    @pytest.mark.asyncio
    async def test_async_run_returns_immediately(self):
        provider = MockProvider()
        provider.enqueue_text("async result", stop="end_turn")
        transport = _make_transport(provider)

        run, queue = await transport.create_run(
            agent_name="test-agent",
            input_messages=[_user_msg("go async")],
            mode=RunMode.ASYNC,
        )

        assert queue is None
        assert run.status == RunStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_async_run_eventually_completes(self):
        provider = MockProvider()
        provider.enqueue_text("done async", stop="end_turn")
        transport = _make_transport(provider)

        run, _ = await transport.create_run(
            agent_name="test-agent",
            input_messages=[_user_msg("go async")],
            mode=RunMode.ASYNC,
        )

        record = transport._runs[run.run_id]
        assert record.task is not None
        await record.task  # wait for background task

        assert run.status == RunStatus.COMPLETED


class TestAcpTransportStream:
    @pytest.mark.asyncio
    async def test_stream_emits_run_created(self):
        provider = MockProvider()
        provider.enqueue_text("streamed!", stop="end_turn")
        transport = _make_transport(provider)

        run, queue = await transport.create_run(
            agent_name="test-agent",
            input_messages=[_user_msg("stream it")],
            mode=RunMode.STREAM,
        )

        assert queue is not None
        events = []
        while True:
            evt = await queue.get()
            if evt is None:
                break
            events.append(evt)

        types = [e.type for e in events]
        assert "run_in_progress" in types
        assert "run_completed" in types
        # Should have at least one message_created
        assert "message_created" in types

    @pytest.mark.asyncio
    async def test_stream_run_completed_event_has_session_id(self):
        provider = MockProvider()
        provider.enqueue_text("done", stop="end_turn")
        transport = _make_transport(provider)

        run, queue = await transport.create_run(
            agent_name="test-agent",
            input_messages=[_user_msg("go")],
            mode=RunMode.STREAM,
        )

        events = []
        while True:
            evt = await queue.get()
            if evt is None:
                break
            events.append(evt)

        completed = next(e for e in events if isinstance(e, RunCompletedEvent))
        assert completed.run.session_id is not None

    @pytest.mark.asyncio
    async def test_stream_message_content(self):
        provider = MockProvider()
        provider.enqueue_text("the answer is 42", stop="end_turn")
        transport = _make_transport(provider)

        run, queue = await transport.create_run(
            agent_name="test-agent",
            input_messages=[_user_msg("what is the answer?")],
            mode=RunMode.STREAM,
        )

        messages = []
        while True:
            evt = await queue.get()
            if evt is None:
                break
            if hasattr(evt, "message"):
                messages.append(evt.message)

        combined = " ".join(m.text for m in messages)
        assert "42" in combined


class TestAcpTransportCancel:
    @pytest.mark.asyncio
    async def test_cancel_unknown_run_returns_none(self):
        config = _make_config()
        transport = AcpTransport(config=config)
        result = await transport.cancel_run("nonexistent-run-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_cancel_completed_run_is_noop(self):
        provider = MockProvider()
        provider.enqueue_text("finished", stop="end_turn")
        transport = _make_transport(provider)

        run, _ = await transport.create_run(
            agent_name="test-agent",
            input_messages=[_user_msg("hi")],
            mode=RunMode.SYNC,
        )
        assert run.status == RunStatus.COMPLETED

        # Cancel after completion should not change status
        cancelled = await transport.cancel_run(run.run_id)
        assert cancelled is not None
        assert cancelled.status == RunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_cancel_async_run(self):
        """Cancelling an in-progress async run should transition it to cancelled."""
        provider = MockProvider()

        # Use a provider that blocks long enough for us to cancel
        async def _slow_complete(messages, tools=None, system=""):
            await asyncio.sleep(10)
            from agent.core.events import ProviderMeta
            from agent.providers.base import ProviderResponse

            return ProviderResponse(
                content="never",
                stop_reason="end_turn",
                meta=ProviderMeta(provider="mock", model="mock-1", usage={}),
            )

        provider.complete = _slow_complete  # type: ignore[method-assign]
        transport = _make_transport(provider)

        run, _ = await transport.create_run(
            agent_name="test-agent",
            input_messages=[_user_msg("slow task")],
            mode=RunMode.ASYNC,
        )

        # Give the task a moment to start
        await asyncio.sleep(0.05)
        cancelled_run = await transport.cancel_run(run.run_id)

        assert cancelled_run is not None
        assert cancelled_run.status == RunStatus.CANCELLED


class TestAcpTransportGetRunEvents:
    @pytest.mark.asyncio
    async def test_get_events_unknown_run(self):
        config = _make_config()
        transport = AcpTransport(config=config)
        assert transport.get_run_events("nope") is None

    @pytest.mark.asyncio
    async def test_events_order(self):
        provider = MockProvider()
        provider.enqueue_text("ordered", stop="end_turn")
        transport = _make_transport(provider)

        run, _ = await transport.create_run(
            agent_name="test-agent",
            input_messages=[_user_msg("order test")],
            mode=RunMode.SYNC,
        )

        events = transport.get_run_events(run.run_id)
        assert events is not None
        # First event must always be run_created
        assert events[0].type == "run_created"
        # Last event must be run_completed (or failed/cancelled)
        assert events[-1].type in ("run_completed", "run_failed", "run_cancelled")


class TestAcpTransportSession:
    def test_get_session_unknown(self, tmp_path):
        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        transport = AcpTransport(config=config)
        assert transport.get_session("no-such-id") is None

    @pytest.mark.asyncio
    async def test_session_id_persists_across_runs(self, tmp_path):
        provider = MockProvider()
        provider.enqueue_text("first reply", stop="end_turn")
        provider.enqueue_text("second reply", stop="end_turn")

        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        transport = AcpTransport(config=config, agent_name="test-agent")

        def patched_make():
            from agent.core.agent import Agent

            return Agent(
                config=transport.config,
                provider=provider,
                approval_callback=transport.approval_callback,
                registry=transport.registry,
            )

        transport._make_agent = patched_make  # type: ignore[method-assign]

        run1, _ = await transport.create_run(
            agent_name="test-agent",
            input_messages=[_user_msg("hello")],
            mode=RunMode.SYNC,
        )
        session_id = run1.session_id
        assert session_id is not None

        # Second run continues the same session
        run2, _ = await transport.create_run(
            agent_name="test-agent",
            input_messages=[_user_msg("follow-up")],
            mode=RunMode.SYNC,
            session_id=session_id,
        )
        info = transport.get_session(run2.session_id)  # type: ignore[arg-type]
        assert info is not None
        assert info["step_count"] >= 2


# ---------------------------------------------------------------------------
# ASGI app integration tests
# ---------------------------------------------------------------------------


async def _call_asgi(
    app: Any,
    method: str,
    path: str,
    body: dict | None = None,
) -> tuple[int, dict]:
    """Minimal ASGI test client — drives the app without a real HTTP server."""

    body_bytes = json.dumps(body).encode() if body else b""

    scope = {
        "type": "http",
        "method": method.upper(),
        "path": path,
        "query_string": b"",
        "headers": [[b"content-type", b"application/json"]],
    }

    response_started: list[dict] = []
    response_body_parts: list[bytes] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            response_started.append(message)
        elif message["type"] == "http.response.body":
            response_body_parts.append(message.get("body", b""))

    await app(scope, receive, send)

    status = response_started[0]["status"] if response_started else 500
    full_body = b"".join(response_body_parts)
    try:
        data = json.loads(full_body)
    except json.JSONDecodeError:
        data = {"_raw": full_body.decode(errors="replace")}
    return status, data


class TestAcpAsgiApp:
    pass

    @pytest.mark.asyncio
    async def test_ping(self):
        config = _make_config()
        app = create_acp_asgi_app(config=config)
        status, data = await _call_asgi(app, "GET", "/ping")
        assert status == 200
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_get_agents(self):
        config = _make_config()
        app = create_acp_asgi_app(config=config, agent_name="aar")
        status, data = await _call_asgi(app, "GET", "/agents")
        assert status == 200
        assert len(data["agents"]) == 1
        assert data["agents"][0]["name"] == "aar"

    @pytest.mark.asyncio
    async def test_get_agent_by_name(self):
        config = _make_config()
        app = create_acp_asgi_app(config=config, agent_name="aar")
        status, data = await _call_asgi(app, "GET", "/agents/aar")
        assert status == 200
        assert data["name"] == "aar"

    @pytest.mark.asyncio
    async def test_get_agent_not_found(self):
        config = _make_config()
        app = create_acp_asgi_app(config=config, agent_name="aar")
        status, data = await _call_asgi(app, "GET", "/agents/other")
        assert status == 404

    @pytest.mark.asyncio
    async def test_post_runs_invalid_json(self):
        config = _make_config()
        app = create_acp_asgi_app(config=config)

        scope = {"type": "http", "method": "POST", "path": "/runs", "headers": []}
        received: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": b"not json", "more_body": False}

        async def send(msg):
            received.append(msg)

        await app(scope, receive, send)
        status = received[0]["status"]
        assert status == 400

    @pytest.mark.asyncio
    async def test_post_runs_invalid_mode(self):
        config = _make_config()
        app = create_acp_asgi_app(config=config)
        status, data = await _call_asgi(
            app,
            "POST",
            "/runs",
            body={
                "agent_name": "aar",
                "input": [{"role": "user", "parts": [{"content": "hi"}]}],
                "mode": "invalid_mode",
            },
        )
        assert status == 400

    @pytest.mark.asyncio
    async def test_get_run_not_found(self):
        config = _make_config()
        app = create_acp_asgi_app(config=config)
        status, data = await _call_asgi(app, "GET", "/runs/nonexistent-id")
        assert status == 404

    @pytest.mark.asyncio
    async def test_get_run_events_not_found(self):
        config = _make_config()
        app = create_acp_asgi_app(config=config)
        status, data = await _call_asgi(app, "GET", "/runs/nope/events")
        assert status == 404

    @pytest.mark.asyncio
    async def test_cancel_run_not_found(self):
        config = _make_config()
        app = create_acp_asgi_app(config=config)
        status, data = await _call_asgi(app, "POST", "/runs/nope/cancel")
        assert status == 404

    @pytest.mark.asyncio
    async def test_unknown_route(self):
        config = _make_config()
        app = create_acp_asgi_app(config=config)
        status, data = await _call_asgi(app, "GET", "/not-a-route")
        assert status == 404

    @pytest.mark.asyncio
    async def test_options_cors_preflight(self):
        config = _make_config()
        app = create_acp_asgi_app(config=config)

        scope = {"type": "http", "method": "OPTIONS", "path": "/runs", "headers": []}
        received: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            received.append(msg)

        await app(scope, receive, send)
        assert received[0]["status"] == 204


# ---------------------------------------------------------------------------
# Helper: _collect_output
# ---------------------------------------------------------------------------


class TestCollectOutput:
    def test_collect_output_plain(self):
        from agent.core.events import AssistantMessage
        from agent.core.session import Session

        session = Session()
        session.append(AssistantMessage(content="first"))
        session.append(AssistantMessage(content="second"))

        output = _collect_output(session)
        assert len(output) == 2
        assert output[0].text == "first"
        assert output[1].text == "second"

    def test_collect_output_skips_empty(self):
        from agent.core.events import AssistantMessage
        from agent.core.session import Session

        session = Session()
        session.append(AssistantMessage(content=""))
        session.append(AssistantMessage(content="non-empty"))

        output = _collect_output(session)
        assert len(output) == 1
        assert output[0].text == "non-empty"
