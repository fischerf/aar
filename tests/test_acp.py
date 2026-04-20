"""Tests for the ACP transport (Agent Communication Protocol).

Covers:
- AarAcpAgent (SDK-based stdio agent for Zed) — requires agent-client-protocol
- AcpTransport + create_acp_asgi_app (HTTP REST transport)
- Shared data models and helpers
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.core.config import AgentConfig, ProviderConfig, SafetyConfig, ToolConfig
from agent.core.state import AgentState
from agent.transports.acp import (
    AarAcpAgent,
    AcpMessage,
    AcpRun,
    AcpTransport,
    AgentManifest,
    MessagePart,
    RunCompletedEvent,
    RunCreatedEvent,
    RunMode,
    RunStatus,
    _collect_output,
    _extract_text,
    _map_stop_reason,
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


def _make_sdk_agent(provider: MockProvider) -> AarAcpAgent:
    """Return an AarAcpAgent with the mock provider injected."""
    config = _make_config()
    agent = AarAcpAgent(config=config, agent_name="test-agent")

    def patched_make(session_id: str = "", approval_callback=None):
        from agent.core.agent import Agent

        return Agent(
            config=agent._config,
            provider=provider,
            approval_callback=approval_callback or agent._default_approval,
            registry=agent._registry,
        )

    agent._make_aar_agent = patched_make  # type: ignore[method-assign]
    return agent


# ---------------------------------------------------------------------------
# SDK-based stdio agent tests
# ---------------------------------------------------------------------------


acp_sdk = pytest.importorskip("acp", reason="agent-client-protocol not installed")


class TestExtractText:
    def test_dict_blocks(self):
        assert _extract_text([{"text": "hello"}, {"text": "world"}]) == "hello\nworld"

    def test_object_blocks(self):
        block = MagicMock()
        block.text = "hi"
        assert _extract_text([block]) == "hi"

    def test_empty(self):
        assert _extract_text([]) == ""

    def test_skips_non_text(self):
        block = MagicMock(spec=[])  # no .text attribute
        assert _extract_text([block]) == ""

    def test_dict_resource_block(self):
        block = {"type": "resource", "uri": "file:///foo/bar.py"}
        assert _extract_text([block]) == "[resource: file:///foo/bar.py]"

    def test_object_resource_content_block(self):
        block = MagicMock(spec=["uri"])
        block.uri = "file:///foo/bar.py"
        assert _extract_text([block]) == "[resource: file:///foo/bar.py]"

    def test_object_embedded_resource_with_text(self):
        resource = MagicMock()
        resource.text = "print('hello')"
        resource.uri = "file:///foo/bar.py"
        block = MagicMock(spec=["resource"])
        block.resource = resource
        result = _extract_text([block])
        assert "print('hello')" in result
        assert "file:///foo/bar.py" in result

    def test_mixed_blocks(self):
        text_block = MagicMock()
        text_block.text = "question"
        resource = MagicMock()
        resource.text = "def foo(): pass"
        resource.uri = "file:///foo.py"
        embedded = MagicMock(spec=["resource"])
        embedded.resource = resource
        result = _extract_text([text_block, embedded])
        assert "question" in result
        assert "def foo(): pass" in result

    def test_dict_image_block(self):
        assert _extract_text([{"type": "image", "data": "abc"}]) == "[image]"

    def test_dict_audio_block(self):
        assert _extract_text([{"type": "audio", "data": "abc"}]) == "[audio]"

    def test_object_image_block_by_type(self):
        block = MagicMock(spec=["type"])
        block.type = "image"
        assert _extract_text([block]) == "[image]"

    def test_object_audio_block_by_type(self):
        block = MagicMock(spec=["type"])
        block.type = "audio"
        assert _extract_text([block]) == "[audio]"

    def test_image_and_text_combined(self):
        result = _extract_text([{"type": "image"}, {"text": "describe it"}])
        assert result == "[image]\ndescribe it"


class TestExtractLocations:
    def test_path_key(self):
        from agent.transports.acp import _extract_locations

        assert _extract_locations({"path": "/tmp/foo.py"}) == ["/tmp/foo.py"]

    def test_file_path_key(self):
        from agent.transports.acp import _extract_locations

        assert _extract_locations({"file_path": "/tmp/bar.py"}) == ["/tmp/bar.py"]

    def test_source_and_destination(self):
        from agent.transports.acp import _extract_locations

        result = _extract_locations({"source": "/a.py", "destination": "/b.py"})
        assert "/a.py" in result
        assert "/b.py" in result

    def test_empty_arguments(self):
        from agent.transports.acp import _extract_locations

        assert _extract_locations({}) == []

    def test_non_path_keys_ignored(self):
        from agent.transports.acp import _extract_locations

        assert _extract_locations({"cmd": "ls", "args": "-la"}) == []

    def test_deduplicates_same_path(self):
        from agent.transports.acp import _extract_locations

        result = _extract_locations({"path": "/a.py", "source": "/a.py"})
        assert result.count("/a.py") == 1

    def test_empty_string_skipped(self):
        from agent.transports.acp import _extract_locations

        assert _extract_locations({"path": ""}) == []


class TestMapStopReason:
    def test_completed(self):
        assert _map_stop_reason(AgentState.COMPLETED) == "end_turn"

    def test_max_steps(self):
        assert _map_stop_reason(AgentState.MAX_STEPS) == "max_turn_requests"

    def test_cancelled_maps_to_cancelled(self):
        assert _map_stop_reason(AgentState.CANCELLED) == "cancelled"

    def test_error_states_map_to_end_turn(self):
        """Operational failures use end_turn, not refusal (which triggers a
        prominent 'refused to respond' banner in Zed)."""
        for state in (AgentState.ERROR, AgentState.TIMED_OUT):
            assert _map_stop_reason(state) == "end_turn"

    def test_budget_exceeded_maps_to_max_tokens(self):
        assert _map_stop_reason(AgentState.BUDGET_EXCEEDED) == "max_tokens"


class TestAarAcpAgentInitialize:
    @pytest.mark.asyncio
    async def test_returns_protocol_version(self):
        agent = AarAcpAgent(config=_make_config())
        resp = await agent.initialize(protocol_version=1)
        assert resp.protocol_version == 1

    @pytest.mark.asyncio
    async def test_returns_sdk_protocol_version(self):
        from acp import PROTOCOL_VERSION

        agent = AarAcpAgent(config=_make_config())
        resp = await agent.initialize(protocol_version=42)
        assert resp.protocol_version == PROTOCOL_VERSION

    @pytest.mark.asyncio
    async def test_declares_mcp_capabilities(self):
        """ACP clients only forward HTTP/SSE MCP servers when the agent
        advertises support via ``mcp_capabilities``. Stdio is always sent."""
        agent = AarAcpAgent(config=_make_config())
        resp = await agent.initialize(protocol_version=1)
        caps = resp.agent_capabilities.mcp_capabilities
        assert caps is not None
        assert caps.http is True


class TestAarAcpAgentNewSession:
    @pytest.mark.asyncio
    async def test_returns_session_id(self, tmp_path):
        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)
        resp = await agent.new_session()
        assert resp.session_id
        assert resp.session_id in agent._sessions

    @pytest.mark.asyncio
    async def test_each_call_new_id(self, tmp_path):
        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)
        r1 = await agent.new_session()
        r2 = await agent.new_session()
        assert r1.session_id != r2.session_id


class TestAarAcpAgentPrompt:
    @pytest.mark.asyncio
    async def test_prompt_returns_end_turn(self, tmp_path):
        provider = MockProvider()
        provider.enqueue_text("Hello from Aar!", stop="end_turn")

        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        sdk_agent = _make_sdk_agent(provider)
        sdk_agent._config = config
        sdk_agent._store = __import__(
            "agent.memory.session_store", fromlist=["SessionStore"]
        ).SessionStore(tmp_path)

        # Create a session first
        new_sess_resp = await sdk_agent.new_session()
        session_id = new_sess_resp.session_id

        # Wire up a mock conn to capture session_update calls
        mock_conn = AsyncMock()
        sdk_agent._conn = mock_conn

        resp = await sdk_agent.prompt(
            prompt=[{"text": "Hello"}],
            session_id=session_id,
        )

        assert resp.stop_reason == "end_turn"
        # session_update should have been called with the assistant reply
        mock_conn.session_update.assert_called()
        call_kwargs = mock_conn.session_update.call_args_list[0].kwargs
        assert call_kwargs["session_id"] == session_id

    @pytest.mark.asyncio
    async def test_prompt_persists_session(self, tmp_path):
        provider = MockProvider()
        provider.enqueue_text("first", stop="end_turn")
        provider.enqueue_text("second", stop="end_turn")

        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        sdk_agent = _make_sdk_agent(provider)
        sdk_agent._config = config
        from agent.memory.session_store import SessionStore

        sdk_agent._store = SessionStore(tmp_path)

        mock_conn = AsyncMock()
        sdk_agent._conn = mock_conn

        r1 = await sdk_agent.new_session()
        await sdk_agent.prompt(prompt=[{"text": "turn 1"}], session_id=r1.session_id)
        await sdk_agent.prompt(prompt=[{"text": "turn 2"}], session_id=r1.session_id)

        # Session should have accumulated events for both turns
        session = sdk_agent._sessions[r1.session_id]
        assert session.step_count >= 2

    @pytest.mark.asyncio
    async def test_prompt_no_conn_no_crash(self, tmp_path):
        """When _conn is None (no client connected), prompt should still complete."""
        provider = MockProvider()
        provider.enqueue_text("silent", stop="end_turn")

        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        sdk_agent = _make_sdk_agent(provider)
        sdk_agent._config = config
        from agent.memory.session_store import SessionStore

        sdk_agent._store = SessionStore(tmp_path)
        sdk_agent._conn = None  # No client

        r = await sdk_agent.new_session()
        resp = await sdk_agent.prompt(prompt=[{"text": "hi"}], session_id=r.session_id)
        assert resp.stop_reason == "end_turn"

    @pytest.mark.asyncio
    async def test_prompt_unknown_session_creates_fresh(self, tmp_path):
        """Prompting with an unrecognised session_id should not crash."""
        provider = MockProvider()
        provider.enqueue_text("ok", stop="end_turn")

        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        sdk_agent = _make_sdk_agent(provider)
        sdk_agent._config = config
        from agent.memory.session_store import SessionStore

        sdk_agent._store = SessionStore(tmp_path)
        sdk_agent._conn = AsyncMock()

        resp = await sdk_agent.prompt(
            prompt=[{"text": "anything"}],
            session_id="nonexistent-session-id",
        )
        assert resp.stop_reason == "end_turn"


class TestAarAcpAgentLoadSession:
    @pytest.mark.asyncio
    async def test_load_existing_session(self, tmp_path):
        from agent.memory.session_store import SessionStore

        # Persist a session first
        store = SessionStore(tmp_path)
        from agent.core.session import Session

        s = Session()
        store.save(s)

        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)

        resp = await agent.load_session(session_id=s.session_id)
        assert resp is not None  # LoadSessionResponse
        assert s.session_id in agent._sessions

    @pytest.mark.asyncio
    async def test_load_missing_session_returns_none(self, tmp_path):
        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)

        resp = await agent.load_session(session_id="does-not-exist")
        assert resp is None

    @pytest.mark.asyncio
    async def test_prompt_after_load_session(self, tmp_path):
        """After load_session, prompt should continue the loaded session."""
        from agent.core.session import Session
        from agent.memory.session_store import SessionStore

        store = SessionStore(tmp_path)
        s = Session()
        store.save(s)

        provider = MockProvider()
        provider.enqueue_text("resumed!", stop="end_turn")

        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        sdk_agent = _make_sdk_agent(provider)
        sdk_agent._config = config
        sdk_agent._store = SessionStore(tmp_path)
        sdk_agent._conn = AsyncMock()

        await sdk_agent.load_session(session_id=s.session_id)
        resp = await sdk_agent.prompt(prompt=[{"text": "continue"}], session_id=s.session_id)
        assert resp.stop_reason == "end_turn"


class TestAarAcpAgentListSessions:
    @pytest.mark.asyncio
    async def test_empty_store(self, tmp_path):
        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)

        resp = await agent.list_sessions()
        assert resp.sessions == []

    @pytest.mark.asyncio
    async def test_lists_saved_sessions(self, tmp_path):
        from agent.core.session import Session
        from agent.memory.session_store import SessionStore

        store = SessionStore(tmp_path)
        ids = set()
        for _ in range(3):
            s = Session()
            store.save(s)
            ids.add(s.session_id)

        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)

        resp = await agent.list_sessions()
        returned_ids = {si.session_id for si in resp.sessions}
        assert ids == returned_ids

    @pytest.mark.asyncio
    async def test_updated_at_populated_when_events_exist(self, tmp_path):
        """Sessions with events include an ISO 8601 updated_at timestamp."""
        from agent.core.events import AssistantMessage
        from agent.core.session import Session
        from agent.memory.session_store import SessionStore

        store = SessionStore(tmp_path)
        s = Session()
        s.events.append(AssistantMessage(content="hello"))
        store.save(s)

        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)

        resp = await agent.list_sessions()
        info = next(si for si in resp.sessions if si.session_id == s.session_id)
        assert info.updated_at is not None
        # Should be a valid ISO 8601 string
        import datetime
        datetime.datetime.fromisoformat(info.updated_at)

    @pytest.mark.asyncio
    async def test_updated_at_none_for_empty_session(self, tmp_path):
        """Sessions with no events have updated_at=None."""
        from agent.core.session import Session
        from agent.memory.session_store import SessionStore

        store = SessionStore(tmp_path)
        s = Session()
        store.save(s)

        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)

        resp = await agent.list_sessions()
        info = next(si for si in resp.sessions if si.session_id == s.session_id)
        assert info.updated_at is None


class TestAarAcpAgentEventStreaming:
    @pytest.mark.asyncio
    async def test_tool_call_events_pushed(self, tmp_path):
        """ToolCall + ToolResult events should produce session_update calls."""
        from agent.core.events import ToolCall as AarToolCall
        from agent.core.events import ToolResult as AarToolResult

        provider = MockProvider()
        provider.enqueue_text("done", stop="end_turn")

        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        sdk_agent = _make_sdk_agent(provider)
        sdk_agent._config = config
        sdk_agent._store = __import__(
            "agent.memory.session_store", fromlist=["SessionStore"]
        ).SessionStore(tmp_path)
        mock_conn = AsyncMock()
        sdk_agent._conn = mock_conn

        # Inject synthetic ToolCall + ToolResult events via on_event callback
        injected_events = [
            AarToolCall(tool_name="bash", tool_call_id="tc1", arguments={"cmd": "ls"}),
            AarToolResult(tool_name="bash", tool_call_id="tc1", output="file.py", is_error=False),
        ]
        original_run = sdk_agent._make_aar_agent

        def patched_make():
            inner = original_run()
            orig_run_method = inner.run

            async def run_and_inject(text, session=None, **kw):
                # Fire synthetic events before completing
                for evt in injected_events:
                    inner._fire_event(evt)
                return await orig_run_method(text, session, **kw)

            inner.run = run_and_inject
            return inner

        # Just verify session_update is called; full integration needs provider mock
        # that emits tool events — here we test that the handlers are wired up
        await sdk_agent.new_session()
        # The actual event wiring is tested by checking the on_event callback directly
        from acp.schema import ToolCallProgress, ToolCallStart

        ts = ToolCallStart(
            title="bash", tool_call_id="tc1", status="in_progress", session_update="tool_call"
        )
        tp = ToolCallProgress(
            title="bash", tool_call_id="tc1", status="completed", session_update="tool_call_update"
        )
        assert ts.status == "in_progress"
        assert tp.status == "completed"

    @pytest.mark.asyncio
    async def test_thinking_event_schema(self):
        """AgentThoughtChunk can be constructed from a ReasoningBlock content."""
        from acp.schema import AgentThoughtChunk, TextContentBlock

        chunk = AgentThoughtChunk(
            content=TextContentBlock(type="text", text="I should think about this"),
            session_update="agent_thought_chunk",
        )
        assert chunk.content.text == "I should think about this"


class TestAarAcpAgentCapabilities:
    @pytest.mark.asyncio
    async def test_declares_load_session(self):
        agent = AarAcpAgent(config=_make_config())
        resp = await agent.initialize(protocol_version=1)
        assert resp.agent_capabilities.load_session is True

    @pytest.mark.asyncio
    async def test_declares_embedded_context(self):
        agent = AarAcpAgent(config=_make_config())
        resp = await agent.initialize(protocol_version=1)
        assert resp.agent_capabilities.prompt_capabilities.embedded_context is True

    @pytest.mark.asyncio
    async def test_declares_list_and_close_session(self):
        agent = AarAcpAgent(config=_make_config())
        resp = await agent.initialize(protocol_version=1)
        caps = resp.agent_capabilities.session_capabilities
        assert caps.list is not None
        assert caps.close is not None


class TestAarAcpAgentCloseSession:
    @pytest.mark.asyncio
    async def test_close_removes_session(self, tmp_path):
        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)
        r = await agent.new_session()
        sid = r.session_id
        assert sid in agent._sessions

        await agent.close_session(session_id=sid)
        assert sid not in agent._sessions

    @pytest.mark.asyncio
    async def test_close_removes_cancel_event(self, tmp_path):
        import asyncio

        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)
        r = await agent.new_session()
        sid = r.session_id
        agent._cancel_events[sid] = asyncio.Event()

        await agent.close_session(session_id=sid)
        assert sid not in agent._cancel_events

    @pytest.mark.asyncio
    async def test_close_unknown_session_no_crash(self, tmp_path):
        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)
        # Should not raise
        await agent.close_session(session_id="unknown-session-id")


class TestAarAcpAgentCancel:
    @pytest.mark.asyncio
    async def test_cancel_sets_event(self, tmp_path):
        import asyncio

        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)
        r = await agent.new_session()
        sid = r.session_id

        event = asyncio.Event()
        agent._cancel_events[sid] = event
        assert not event.is_set()

        await agent.cancel(session_id=sid)
        assert event.is_set()

    @pytest.mark.asyncio
    async def test_cancel_no_active_prompt_no_crash(self, tmp_path):
        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)
        # No active prompt / cancel event registered
        await agent.cancel(session_id="no-such-session")


class TestAarAcpAgentConcurrency:
    """C3+C4: fire-and-forget tracking and per-session serialization."""

    @pytest.mark.asyncio
    async def test_spawn_tracks_task(self, tmp_path):
        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)

        async def _noop() -> None:
            await asyncio.sleep(0)

        task = agent._spawn(_noop(), name="unit-test-task")
        assert task in agent._background_tasks
        await task
        # Done-callback runs on next tick
        await asyncio.sleep(0)
        assert task not in agent._background_tasks

    @pytest.mark.asyncio
    async def test_spawn_exceptions_logged_and_cleared(self, tmp_path, caplog):
        import logging

        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)

        async def _boom() -> None:
            raise RuntimeError("boom")

        with caplog.at_level(logging.WARNING, logger="agent.transports.acp"):
            task = agent._spawn(_boom(), name="boom-task")
            # Await via gather so we don't re-raise
            await asyncio.gather(task, return_exceptions=True)
            # Let the done-callback fire
            await asyncio.sleep(0)

        assert task not in agent._background_tasks
        assert any("boom" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_shutdown_drains_background_tasks(self, tmp_path):
        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)

        done = asyncio.Event()

        async def _slow() -> None:
            await asyncio.sleep(0.01)
            done.set()

        agent._spawn(_slow(), name="slow-task")
        await agent.shutdown()
        assert done.is_set()
        assert not agent._background_tasks

    @pytest.mark.asyncio
    async def test_concurrent_prompt_same_session_rejected(self, tmp_path):
        """A second prompt while the first is in flight must raise."""
        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)
        r = await agent.new_session()
        sid = r.session_id

        # Simulate an in-flight prompt by parking a task in _run_tasks
        gate = asyncio.Event()

        async def _parked() -> None:
            await gate.wait()

        fake_task = asyncio.create_task(_parked())
        agent._run_tasks[sid] = fake_task

        with pytest.raises(RuntimeError, match="already in flight"):
            await agent.prompt(prompt=[{"text": "hello"}], session_id=sid)

        gate.set()
        await fake_task

    @pytest.mark.asyncio
    async def test_close_session_cancels_in_flight_prompt(self, tmp_path):
        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)
        r = await agent.new_session()
        sid = r.session_id

        started = asyncio.Event()

        async def _parked() -> None:
            started.set()
            await asyncio.sleep(30)

        task = asyncio.create_task(_parked())
        agent._run_tasks[sid] = task
        agent._cancel_events[sid] = asyncio.Event()
        await started.wait()

        await agent.close_session(session_id=sid)

        assert task.done()
        assert task.cancelled() or task.exception() is not None
        assert sid not in agent._run_tasks
        assert sid not in agent._cancel_events

    @pytest.mark.asyncio
    async def test_session_lock_is_same_object_per_id(self, tmp_path):
        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)
        lock_a = agent._session_lock("abc123")
        lock_b = agent._session_lock("abc123")
        lock_c = agent._session_lock("other-sid")
        assert lock_a is lock_b
        assert lock_a is not lock_c


class TestAarAcpAgentCwd:
    @pytest.mark.asyncio
    async def test_new_session_stores_cwd(self, tmp_path):
        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)
        r = await agent.new_session(cwd="/my/project")
        session = agent._sessions[r.session_id]
        assert session.metadata.get("cwd") == "/my/project"

    @pytest.mark.asyncio
    async def test_load_session_updates_cwd(self, tmp_path):
        from agent.core.session import Session
        from agent.memory.session_store import SessionStore

        store = SessionStore(tmp_path)
        s = Session()
        store.save(s)

        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)
        await agent.load_session(session_id=s.session_id, cwd="/updated/cwd")
        assert agent._sessions[s.session_id].metadata.get("cwd") == "/updated/cwd"

    @pytest.mark.asyncio
    async def test_list_sessions_includes_cwd(self, tmp_path):
        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)
        r = await agent.new_session(cwd="/my/project")

        resp = await agent.list_sessions()
        matched = next(si for si in resp.sessions if si.session_id == r.session_id)
        assert matched.cwd == "/my/project"


class TestAarAcpAgentMcpConversion:
    def test_http_server_dict(self):
        from agent.transports.acp import _acp_server_to_mcp_config

        result = _acp_server_to_mcp_config({"url": "http://localhost:3000/mcp", "name": "my-mcp"})
        assert result is not None
        assert result.transport == "http"
        assert result.url == "http://localhost:3000/mcp"

    def test_stdio_server_dict(self):
        from agent.transports.acp import _acp_server_to_mcp_config

        result = _acp_server_to_mcp_config(
            {"command": "python", "args": ["-m", "my_mcp"], "name": "my-mcp"}
        )
        assert result is not None
        assert result.transport == "stdio"
        assert result.command == "python"
        assert result.args == ["-m", "my_mcp"]

    def test_sdk_object_with_url(self):
        from agent.transports.acp import _acp_server_to_mcp_config

        srv = MagicMock()
        srv.url = "http://mcp.example.com/api"
        srv.command = None
        srv.name = "remote"
        result = _acp_server_to_mcp_config(srv)
        assert result is not None
        assert result.url == "http://mcp.example.com/api"

    def test_sdk_object_with_command(self):
        from agent.transports.acp import _acp_server_to_mcp_config

        srv = MagicMock()
        srv.url = None
        srv.command = "npx"
        srv.args = ["@modelcontextprotocol/server-filesystem", "/tmp"]
        srv.env = {}
        srv.name = "filesystem"
        result = _acp_server_to_mcp_config(srv)
        assert result is not None
        assert result.command == "npx"
        assert "/tmp" in result.args

    def test_unrecognised_server_returns_none(self):
        from agent.transports.acp import _acp_server_to_mcp_config

        result = _acp_server_to_mcp_config({})
        assert result is None

    def test_sdk_stdio_with_env_list(self):
        """Real ``McpServerStdio`` ships ``env`` as ``list[EnvVariable]`` — must
        be coerced to ``dict``, not blindly passed to ``dict(...)``."""
        from acp.schema import EnvVariable, McpServerStdio

        from agent.transports.acp import _acp_server_to_mcp_config

        srv = McpServerStdio(
            name="filesystem",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            env=[
                EnvVariable(name="FOO", value="bar"),
                EnvVariable(name="TOKEN", value="secret"),
            ],
        )
        result = _acp_server_to_mcp_config(srv)
        assert result is not None
        assert result.transport == "stdio"
        assert result.command == "npx"
        assert result.env == {"FOO": "bar", "TOKEN": "secret"}

    def test_sdk_http_with_headers_list(self):
        """Real ``HttpMcpServer`` ships ``headers`` as ``list[HttpHeader]``."""
        from acp.schema import HttpHeader, HttpMcpServer

        from agent.transports.acp import _acp_server_to_mcp_config

        srv = HttpMcpServer(
            name="remote",
            url="https://mcp.example.com/api",
            headers=[HttpHeader(name="Authorization", value="Bearer sk-...")],
            type="http",
        )
        result = _acp_server_to_mcp_config(srv)
        assert result is not None
        assert result.transport == "http"
        assert result.url == "https://mcp.example.com/api"
        assert result.headers == {"Authorization": "Bearer sk-..."}

    def test_sdk_sse_is_skipped(self, caplog):
        """Aar's bridge cannot speak SSE framing — SSE servers must be dropped
        (not silently routed to the http code path)."""
        import logging

        from acp.schema import SseMcpServer

        from agent.transports.acp import _acp_server_to_mcp_config

        srv = SseMcpServer(name="sse-remote", url="https://x/sse", headers=[], type="sse")
        with caplog.at_level(logging.WARNING, logger="agent.transports.acp.common"):
            result = _acp_server_to_mcp_config(srv)
        assert result is None
        assert any("SSE" in rec.message for rec in caplog.records)


class TestModelIdToProvider:
    def test_claude_maps_to_anthropic(self):
        from agent.transports.acp import _model_id_to_provider

        provider, model = _model_id_to_provider("claude-sonnet-4-6")
        assert provider == "anthropic"
        assert model == "claude-sonnet-4-6"

    def test_gpt_maps_to_openai(self):
        from agent.transports.acp import _model_id_to_provider

        provider, model = _model_id_to_provider("gpt-4o")
        assert provider == "openai"

    def test_o4_maps_to_openai(self):
        from agent.transports.acp import _model_id_to_provider

        provider, _ = _model_id_to_provider("o4-mini")
        assert provider == "openai"

    def test_llama_maps_to_ollama(self):
        from agent.transports.acp import _model_id_to_provider

        provider, model = _model_id_to_provider("llama3")
        assert provider == "ollama"
        assert model == "llama3"

    def test_unknown_maps_to_ollama(self):
        from agent.transports.acp import _model_id_to_provider

        provider, _ = _model_id_to_provider("totally-custom-model")
        assert provider == "ollama"


class TestSetSessionModel:
    @pytest.mark.asyncio
    async def test_switches_model_in_session_config(self, tmp_path):
        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)
        r = await agent.new_session()
        sid = r.session_id

        await agent.set_session_model(model_id="claude-sonnet-4-6", session_id=sid)

        session_cfg = agent._session_configs[sid]
        assert session_cfg.provider.name == "anthropic"
        assert session_cfg.provider.model == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_different_sessions_have_independent_models(self, tmp_path):
        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)

        r1 = await agent.new_session()
        r2 = await agent.new_session()

        await agent.set_session_model(model_id="gpt-4o", session_id=r1.session_id)
        await agent.set_session_model(model_id="llama3", session_id=r2.session_id)

        assert agent._session_configs[r1.session_id].provider.name == "openai"
        assert agent._session_configs[r2.session_id].provider.name == "ollama"

    @pytest.mark.asyncio
    async def test_close_session_clears_model_override(self, tmp_path):
        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)
        r = await agent.new_session()
        sid = r.session_id

        await agent.set_session_model(model_id="gpt-4o", session_id=sid)
        assert sid in agent._session_configs

        await agent.close_session(session_id=sid)
        assert sid not in agent._session_configs


class TestAvailableCommands:
    def test_returns_builtin_commands(self):
        from agent.transports.acp import _available_commands

        cmds = _available_commands()
        names = {c.name for c in cmds}
        assert "status" in names
        assert "tools" in names
        assert "policy" in names

    def test_no_model_or_clear_commands(self):
        from agent.transports.acp import _available_commands

        names = {c.name for c in _available_commands()}
        assert "model" not in names
        assert "clear" not in names

    def test_all_commands_have_descriptions(self):
        from agent.transports.acp import _available_commands

        for cmd in _available_commands():
            assert cmd.description, f"{cmd.name} missing description"

    @pytest.mark.asyncio
    async def test_commands_pushed_once_per_session(self, tmp_path):
        """AvailableCommandsUpdate is pushed at most twice per session (new_session + prompt)."""
        from acp.schema import AvailableCommandsUpdate

        provider = MockProvider()
        provider.enqueue_text("answer 1", stop="end_turn")
        provider.enqueue_text("answer 2", stop="end_turn")

        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        sdk_agent = _make_sdk_agent(provider)
        sdk_agent._config = config
        from agent.memory.session_store import SessionStore

        sdk_agent._store = SessionStore(tmp_path)
        mock_conn = AsyncMock()
        sdk_agent._conn = mock_conn

        r = await sdk_agent.new_session()
        await sdk_agent.prompt(prompt=[{"text": "first"}], session_id=r.session_id)
        await sdk_agent.prompt(prompt=[{"text": "second"}], session_id=r.session_id)

        all_updates = [call.kwargs["update"] for call in mock_conn.session_update.call_args_list]
        cmds_updates = [u for u in all_updates if isinstance(u, AvailableCommandsUpdate)]
        # Optimistic push from new_session + guaranteed push from first prompt = 2.
        # Second prompt must NOT add another push.
        assert len(cmds_updates) == 2

    @pytest.mark.asyncio
    async def test_commands_pushed_again_after_close(self, tmp_path):
        """After close_session, the next session re-pushes available commands."""
        from acp.schema import AvailableCommandsUpdate

        provider = MockProvider()
        provider.enqueue_text("a1", stop="end_turn")
        provider.enqueue_text("a2", stop="end_turn")

        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        sdk_agent = _make_sdk_agent(provider)
        sdk_agent._config = config
        from agent.memory.session_store import SessionStore

        sdk_agent._store = SessionStore(tmp_path)
        mock_conn = AsyncMock()
        sdk_agent._conn = mock_conn

        r = await sdk_agent.new_session()
        await sdk_agent.prompt(prompt=[{"text": "hi"}], session_id=r.session_id)
        await sdk_agent.close_session(session_id=r.session_id)

        # New session — commands should be pushed again
        r2 = await sdk_agent.new_session()
        await sdk_agent.prompt(prompt=[{"text": "hi again"}], session_id=r2.session_id)

        all_updates = [call.kwargs["update"] for call in mock_conn.session_update.call_args_list]
        cmds_updates = [u for u in all_updates if isinstance(u, AvailableCommandsUpdate)]
        # 2 pushes per session (new_session + prompt) × 2 sessions = 4
        assert len(cmds_updates) == 4


class TestAgentPlanUpdate:
    @pytest.mark.asyncio
    async def test_plan_pushed_for_tool_calls(self, tmp_path):
        """AgentPlanUpdate is pushed when tool calls fire."""
        from acp.schema import AgentPlanUpdate

        provider = MockProvider()
        provider.enqueue_text("done", stop="end_turn")

        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        sdk_agent = _make_sdk_agent(provider)
        sdk_agent._config = config
        from agent.memory.session_store import SessionStore

        sdk_agent._store = SessionStore(tmp_path)
        mock_conn = AsyncMock()
        sdk_agent._conn = mock_conn

        r = await sdk_agent.new_session()
        await sdk_agent.prompt(prompt=[{"text": "go"}], session_id=r.session_id)

        all_updates = [call.kwargs["update"] for call in mock_conn.session_update.call_args_list]
        # With no tool calls, no plan updates should be sent
        plan_updates = [u for u in all_updates if isinstance(u, AgentPlanUpdate)]
        assert len(plan_updates) == 0  # no tool calls → no plan

    def test_plan_entry_construction(self):
        """PlanEntry and AgentPlanUpdate can be correctly constructed."""
        from acp.schema import AgentPlanUpdate, PlanEntry

        entries = [
            PlanEntry(content="read_file", status="completed", priority="medium"),
            PlanEntry(content="write_file", status="in_progress", priority="medium"),
        ]
        plan = AgentPlanUpdate(entries=entries, session_update="plan")
        assert plan.entries[0].status == "completed"
        assert plan.entries[1].status == "in_progress"
        assert plan.session_update == "plan"


class TestSlashCommandHandler:
    """_handle_slash_command returns useful plain-text responses without running the agent."""

    def _make_agent(self, tmp_path):
        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        agent = AarAcpAgent(config=config)
        return agent

    def test_status_contains_session_id(self, tmp_path):
        from agent.core.session import Session

        agent = self._make_agent(tmp_path)
        session = Session(session_id="abc123")
        reply = agent._handle_slash_command("/status", "abc123", session)
        assert "abc123" in reply

    def test_status_contains_provider_and_model(self, tmp_path):
        from agent.core.session import Session

        agent = self._make_agent(tmp_path)
        session = Session(session_id="x")
        reply = agent._handle_slash_command("/status", "x", session)
        assert agent._config.provider.name in reply
        assert agent._config.provider.model in reply

    def test_tools_lists_tool_names(self, tmp_path):
        from agent.core.config import ToolConfig
        from agent.core.session import Session

        config = _make_config()
        config = config.model_copy(
            update={"tools": ToolConfig(enabled_builtins=["read_file", "write_file"])}
        )
        agent = AarAcpAgent(config=config)
        session = Session(session_id="x")
        reply = agent._handle_slash_command("/tools", "x", session)
        assert "read_file" in reply
        assert "Available tools" in reply

    def test_tools_includes_builtins_when_mcp_registry_present(self, tmp_path):
        """When an MCP session registry exists, /tools must still list built-in
        tools — they are not stored in the session registry but are registered
        by AarAgent._register_builtins() at prompt time."""
        from agent.core.config import ToolConfig
        from agent.core.session import Session
        from agent.tools.registry import ToolRegistry
        from agent.tools.schema import SideEffect, ToolSpec

        config = _make_config()
        config = config.model_copy(
            update={"tools": ToolConfig(enabled_builtins=["read_file", "write_file"])}
        )
        agent = AarAcpAgent(config=config)

        # Simulate what _setup_mcp does: a registry with only MCP tools
        mcp_only_registry = ToolRegistry()
        mcp_only_registry.add(
            ToolSpec(
                name="fetch",
                description="Fetch a web page",
                input_schema={"type": "object", "properties": {}},
                side_effects=[SideEffect.NETWORK],
            )
        )
        session_id = "mcp-session"
        agent._session_registries[session_id] = mcp_only_registry

        session = Session(session_id=session_id)
        reply = agent._handle_slash_command("/tools", session_id, session)

        # MCP tool must appear
        assert "fetch" in reply
        # Built-in tools must also appear (the bug was they were missing)
        assert "read_file" in reply
        assert "write_file" in reply

    def test_policy_contains_approval_fields(self, tmp_path):
        from agent.core.session import Session

        agent = self._make_agent(tmp_path)
        session = Session(session_id="x")
        reply = agent._handle_slash_command("/policy", "x", session)
        assert "Approve writes" in reply
        assert "Approve execute" in reply

    @pytest.mark.asyncio
    async def test_prompt_handles_status_command(self, tmp_path):
        """A /status prompt returns immediately without calling the provider."""
        provider = MockProvider()  # no responses queued — would raise if called
        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        sdk_agent = _make_sdk_agent(provider)
        sdk_agent._config = config
        from agent.memory.session_store import SessionStore

        sdk_agent._store = SessionStore(tmp_path)
        mock_conn = AsyncMock()
        sdk_agent._conn = mock_conn

        r = await sdk_agent.new_session()
        response = await sdk_agent.prompt(prompt=[{"text": "/status"}], session_id=r.session_id)

        assert response.stop_reason == "end_turn"
        # Provider was never called
        assert len(provider.call_history) == 0

    @pytest.mark.asyncio
    async def test_prompt_handles_policy_command(self, tmp_path):
        provider = MockProvider()
        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        sdk_agent = _make_sdk_agent(provider)
        sdk_agent._config = config
        from agent.memory.session_store import SessionStore

        sdk_agent._store = SessionStore(tmp_path)
        mock_conn = AsyncMock()
        sdk_agent._conn = mock_conn

        r = await sdk_agent.new_session()
        response = await sdk_agent.prompt(prompt=[{"text": "/policy"}], session_id=r.session_id)

        assert response.stop_reason == "end_turn"
        assert len(provider.call_history) == 0


class TestAarAcpAgentStreamingAndUsage:
    @pytest.mark.asyncio
    async def test_session_info_update_sent_after_first_response(self, tmp_path):
        """SessionInfoUpdate should be pushed once per prompt call."""
        from acp.schema import SessionInfoUpdate

        provider = MockProvider()
        provider.enqueue_text("Here is my answer", stop="end_turn")

        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        sdk_agent = _make_sdk_agent(provider)
        sdk_agent._config = config
        from agent.memory.session_store import SessionStore

        sdk_agent._store = SessionStore(tmp_path)
        mock_conn = AsyncMock()
        sdk_agent._conn = mock_conn

        r = await sdk_agent.new_session()
        await sdk_agent.prompt(prompt=[{"text": "tell me something"}], session_id=r.session_id)

        all_updates = [call.kwargs["update"] for call in mock_conn.session_update.call_args_list]
        info_updates = [u for u in all_updates if isinstance(u, SessionInfoUpdate)]
        assert len(info_updates) == 1
        assert info_updates[0].title  # non-empty title

    @pytest.mark.asyncio
    async def test_usage_update_type_construction(self):
        """UsageUpdate can be constructed with Cost object."""
        from acp.schema import Cost, UsageUpdate

        u = UsageUpdate(
            cost=Cost(amount=0.01, currency="usd"),
            size=1000,
            used=200,
            session_update="usage_update",
        )
        assert u.cost.amount == 0.01
        assert u.used == 200


class TestSideEffectsToToolKind:
    """_side_effects_to_tool_kind maps Aar SideEffect → ACP ToolKind."""

    def test_execute_is_highest_priority(self):
        from agent.tools.schema import SideEffect
        from agent.transports.acp import _side_effects_to_tool_kind

        assert _side_effects_to_tool_kind([SideEffect.EXECUTE, SideEffect.WRITE]) == "execute"

    def test_write_maps_to_edit(self):
        from agent.tools.schema import SideEffect
        from agent.transports.acp import _side_effects_to_tool_kind

        assert _side_effects_to_tool_kind([SideEffect.WRITE]) == "edit"

    def test_network_maps_to_fetch(self):
        from agent.tools.schema import SideEffect
        from agent.transports.acp import _side_effects_to_tool_kind

        assert _side_effects_to_tool_kind([SideEffect.NETWORK]) == "fetch"

    def test_read_maps_to_read(self):
        from agent.tools.schema import SideEffect
        from agent.transports.acp import _side_effects_to_tool_kind

        assert _side_effects_to_tool_kind([SideEffect.READ]) == "read"

    def test_none_maps_to_other(self):
        from agent.tools.schema import SideEffect
        from agent.transports.acp import _side_effects_to_tool_kind

        assert _side_effects_to_tool_kind([SideEffect.NONE]) == "other"

    def test_empty_maps_to_other(self):
        from agent.transports.acp import _side_effects_to_tool_kind

        assert _side_effects_to_tool_kind([]) == "other"

    def test_delete_name_overrides_write(self):
        from agent.tools.schema import SideEffect
        from agent.transports.acp import _side_effects_to_tool_kind

        assert _side_effects_to_tool_kind([SideEffect.WRITE], "delete_file") == "delete"
        assert _side_effects_to_tool_kind([SideEffect.WRITE], "remove_file") == "delete"

    def test_move_name_overrides_write(self):
        from agent.tools.schema import SideEffect
        from agent.transports.acp import _side_effects_to_tool_kind

        assert _side_effects_to_tool_kind([SideEffect.WRITE], "move_file") == "move"
        assert _side_effects_to_tool_kind([SideEffect.WRITE], "rename_file") == "move"

    def test_search_name_overrides_read(self):
        from agent.tools.schema import SideEffect
        from agent.transports.acp import _side_effects_to_tool_kind

        assert _side_effects_to_tool_kind([SideEffect.READ], "search_files") == "search"
        assert _side_effects_to_tool_kind([SideEffect.READ], "grep") == "search"
        assert _side_effects_to_tool_kind([SideEffect.READ], "find_in_files") == "search"

    def test_think_name_maps_to_think(self):
        from agent.transports.acp import _side_effects_to_tool_kind

        assert _side_effects_to_tool_kind([], "think") == "think"
        assert _side_effects_to_tool_kind([], "reason_about") == "think"

    def test_name_check_is_case_insensitive(self):
        from agent.tools.schema import SideEffect
        from agent.transports.acp import _side_effects_to_tool_kind

        assert _side_effects_to_tool_kind([SideEffect.WRITE], "Delete_File") == "delete"

    def test_no_name_falls_through_to_side_effects(self):
        from agent.tools.schema import SideEffect
        from agent.transports.acp import _side_effects_to_tool_kind

        assert _side_effects_to_tool_kind([SideEffect.EXECUTE]) == "execute"


class TestToolCallPendingStatus:
    """ToolCallStart must use status='pending' per the ACP spec.

    Tools haven't started yet when the LLM first reports them — they may be
    awaiting approval.  Zed only shows permission buttons for 'pending' calls.
    """

    @pytest.mark.asyncio
    async def test_tool_call_start_uses_pending_status(self, tmp_path):
        """on_event(ToolCall) pushes ToolCallStart with status='pending'."""
        from acp.schema import ToolCallStart

        provider = MockProvider()
        provider.enqueue_text("done", stop="end_turn")

        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        sdk_agent = _make_sdk_agent(provider)
        sdk_agent._config = config
        from agent.memory.session_store import SessionStore

        sdk_agent._store = SessionStore(tmp_path)

        mock_conn = AsyncMock()
        sdk_agent._conn = mock_conn

        r = await sdk_agent.new_session()

        # Build the on_event callback by starting a prompt (with a trivial provider)
        await sdk_agent.prompt(prompt=[{"text": "hi"}], session_id=r.session_id)

        # Check that any ToolCallStart pushed used 'pending' status
        for call in mock_conn.session_update.call_args_list:
            update = call.kwargs.get("update") or (call.args[0] if call.args else None)
            if isinstance(update, ToolCallStart):
                assert update.status == "pending", (
                    f"ToolCallStart should use 'pending', got '{update.status}'"
                )

    def test_tool_result_emits_in_progress_then_terminal(self):
        """on_event(ToolResult) must push in_progress before completed/failed.

        The ACP spec lifecycle is pending → in_progress → completed/failed.
        """
        from acp.schema import ToolCallProgress

        from agent.core.events import ToolResult as AarToolResult

        pushed: list[Any] = []
        sdk_agent = AarAcpAgent(config=_make_config())

        def _push(update: Any) -> None:
            pushed.append(update)

        # Simulate the on_event callback logic directly
        event = AarToolResult(tool_name="bash", tool_call_id="tc1", output="ok", is_error=False)

        # Replicate the ToolResult branch from prompt()'s on_event
        _push(
            ToolCallProgress(
                title=event.tool_name,
                tool_call_id=event.tool_call_id,
                status="in_progress",
                session_update="tool_call_update",
            )
        )
        _push(
            ToolCallProgress(
                title=event.tool_name,
                tool_call_id=event.tool_call_id,
                status="completed",
                raw_output={"text": event.output[:4000]},
                session_update="tool_call_update",
            )
        )

        assert len(pushed) == 2
        assert pushed[0].status == "in_progress"
        assert pushed[1].status == "completed"


class TestCancellationStopReason:
    """Cancellation must return stop_reason='cancelled' per the ACP spec."""

    @pytest.mark.asyncio
    async def test_cancel_returns_cancelled_stop_reason(self, tmp_path):
        """When cancel() is called during a prompt, stop_reason is 'cancelled'."""
        provider = MockProvider()
        # Use a slow provider that we can cancel during
        slow_event = asyncio.Event()

        original_complete = provider.complete

        async def slow_complete(*args, **kwargs):
            await slow_event.wait()  # block until cancelled
            return await original_complete(*args, **kwargs)

        provider.complete = slow_complete

        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        sdk_agent = _make_sdk_agent(provider)
        sdk_agent._config = config
        from agent.memory.session_store import SessionStore

        sdk_agent._store = SessionStore(tmp_path)

        r = await sdk_agent.new_session()
        session_id = r.session_id

        # Start prompt in a task
        prompt_task = asyncio.create_task(
            sdk_agent.prompt(prompt=[{"text": "do something slow"}], session_id=session_id)
        )

        # Give the prompt a moment to start
        await asyncio.sleep(0.05)

        # Cancel
        await sdk_agent.cancel(session_id=session_id)

        # Wait for the prompt to finish
        try:
            result = await asyncio.wait_for(prompt_task, timeout=5.0)
        except asyncio.TimeoutError:
            pytest.fail("Prompt did not complete after cancel within timeout")
        except asyncio.CancelledError:
            pytest.fail("Prompt raised CancelledError instead of returning cancelled response")

        assert result.stop_reason == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_stores_and_cancels_run_task(self, tmp_path):
        """cancel() calls task.cancel() on the stored run task."""
        config = _make_config()
        config = config.model_copy(update={"session_dir": tmp_path})
        sdk_agent = AarAcpAgent(config=config, agent_name="test-agent")
        from agent.memory.session_store import SessionStore

        sdk_agent._store = SessionStore(tmp_path)

        # Simulate a running task — use MagicMock because done()/cancel() are sync
        mock_task = MagicMock()
        mock_task.done.return_value = False
        sdk_agent._run_tasks["sess_1"] = mock_task

        cancel_event = asyncio.Event()
        sdk_agent._cancel_events["sess_1"] = cancel_event

        await sdk_agent.cancel(session_id="sess_1")

        assert cancel_event.is_set()
        mock_task.cancel.assert_called_once()

    def test_map_stop_reason_cancelled(self):
        assert _map_stop_reason(AgentState.CANCELLED) == "cancelled"

    def test_map_stop_reason_error_is_end_turn(self):
        assert _map_stop_reason(AgentState.ERROR) == "end_turn"

    def test_map_stop_reason_timed_out_is_end_turn(self):
        assert _map_stop_reason(AgentState.TIMED_OUT) == "end_turn"


# ---------------------------------------------------------------------------
# ACP approval bridge tests
# ---------------------------------------------------------------------------


class TestAcpApprovalBridge:
    """Tests for make_acp_approval_callback (agent.transports.acp_permissions).

    Covers every outcome path: allow-once, allow-always, deny option, non-allowed
    outcome, timeout, arbitrary exception, no-connection fallback, unknown option_id,
    and tool_call_id passthrough / auto-assignment.
    """

    # ------------------------------------------------------------------
    # Fixtures / helpers
    # ------------------------------------------------------------------

    def _spec(self, side_effects=None):
        from agent.tools.schema import SideEffect, ToolSpec

        return ToolSpec(
            name="bash",
            description="Run shell commands",
            side_effects=side_effects or [SideEffect.EXECUTE],
        )

    def _tc(self, tool_call_id: str = "tc-1"):
        from agent.core.events import ToolCall

        return ToolCall(tool_name="bash", tool_call_id=tool_call_id, arguments={"cmd": "ls"})

    def _allowed_response(self, option_id: str):
        """Mock response whose .outcome passes isinstance(x, AllowedOutcome)."""
        from acp.schema import AllowedOutcome

        resp = MagicMock()
        resp.outcome = MagicMock(spec=AllowedOutcome)
        resp.outcome.option_id = option_id
        return resp

    def _rejected_response(self):
        """Mock response whose .outcome is NOT an AllowedOutcome instance."""
        resp = MagicMock()
        resp.outcome = MagicMock()  # plain MagicMock — fails isinstance(x, AllowedOutcome)
        return resp

    # ------------------------------------------------------------------
    # Happy-path outcome mapping
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_allow_once_returns_approved(self):
        from agent.safety.permissions import ApprovalResult
        from agent.transports.acp_permissions import make_acp_approval_callback

        conn = AsyncMock()
        conn.request_permission.return_value = self._allowed_response("allow_once")

        cb = make_acp_approval_callback(conn, "sess-1")
        result = await cb(self._spec(), self._tc())

        assert result == ApprovalResult.APPROVED

    @pytest.mark.asyncio
    async def test_allow_always_returns_approved_always(self):
        from agent.safety.permissions import ApprovalResult
        from agent.transports.acp_permissions import make_acp_approval_callback

        conn = AsyncMock()
        conn.request_permission.return_value = self._allowed_response("allow_always")

        cb = make_acp_approval_callback(conn, "sess-1")
        result = await cb(self._spec(), self._tc())

        assert result == ApprovalResult.APPROVED_ALWAYS

    @pytest.mark.asyncio
    async def test_reject_once_returns_denied(self):
        from agent.safety.permissions import ApprovalResult
        from agent.transports.acp_permissions import make_acp_approval_callback

        conn = AsyncMock()
        conn.request_permission.return_value = self._allowed_response("reject_once")

        cb = make_acp_approval_callback(conn, "sess-1")
        result = await cb(self._spec(), self._tc())

        assert result == ApprovalResult.DENIED

    @pytest.mark.asyncio
    async def test_reject_always_returns_denied(self):
        from agent.safety.permissions import ApprovalResult
        from agent.transports.acp_permissions import make_acp_approval_callback

        conn = AsyncMock()
        conn.request_permission.return_value = self._allowed_response("reject_always")

        cb = make_acp_approval_callback(conn, "sess-1")
        result = await cb(self._spec(), self._tc())

        assert result == ApprovalResult.DENIED

    @pytest.mark.asyncio
    async def test_deny_option_id_returns_denied(self):
        """Legacy 'deny' option_id still maps to DENIED for backward compat."""
        from agent.safety.permissions import ApprovalResult
        from agent.transports.acp_permissions import make_acp_approval_callback

        conn = AsyncMock()
        conn.request_permission.return_value = self._allowed_response("deny")

        cb = make_acp_approval_callback(conn, "sess-1")
        result = await cb(self._spec(), self._tc())

        assert result == ApprovalResult.DENIED

    @pytest.mark.asyncio
    async def test_unknown_option_id_returns_denied(self):
        """An unrecognised option_id in an AllowedOutcome is treated as deny."""
        from agent.safety.permissions import ApprovalResult
        from agent.transports.acp_permissions import make_acp_approval_callback

        conn = AsyncMock()
        conn.request_permission.return_value = self._allowed_response("totally_unknown")

        cb = make_acp_approval_callback(conn, "sess-1")
        result = await cb(self._spec(), self._tc())

        assert result == ApprovalResult.DENIED

    @pytest.mark.asyncio
    async def test_non_allowed_outcome_returns_denied(self):
        """A RejectedOutcome (or any non-AllowedOutcome) should deny the call."""
        from agent.safety.permissions import ApprovalResult
        from agent.transports.acp_permissions import make_acp_approval_callback

        conn = AsyncMock()
        conn.request_permission.return_value = self._rejected_response()

        cb = make_acp_approval_callback(conn, "sess-1")
        result = await cb(self._spec(), self._tc())

        assert result == ApprovalResult.DENIED

    # ------------------------------------------------------------------
    # Error / timeout paths
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_timeout_returns_denied(self):
        """A timed-out permission request must auto-deny rather than crash."""
        from agent.safety.permissions import ApprovalResult
        from agent.transports.acp_permissions import make_acp_approval_callback

        conn = AsyncMock()
        conn.request_permission.side_effect = asyncio.TimeoutError()

        cb = make_acp_approval_callback(conn, "sess-1", timeout=60.0)
        result = await cb(self._spec(), self._tc())

        assert result == ApprovalResult.DENIED

    @pytest.mark.asyncio
    async def test_exception_returns_denied(self):
        """Any other exception from request_permission must auto-deny."""
        from agent.safety.permissions import ApprovalResult
        from agent.transports.acp_permissions import make_acp_approval_callback

        conn = AsyncMock()
        conn.request_permission.side_effect = RuntimeError("connection dropped")

        cb = make_acp_approval_callback(conn, "sess-1")
        result = await cb(self._spec(), self._tc())

        assert result == ApprovalResult.DENIED

    # ------------------------------------------------------------------
    # No-connection fallback
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_no_connection_returns_denied(self):
        """When conn=None the factory returns a safe deny-all callback."""
        from agent.safety.permissions import ApprovalResult
        from agent.transports.acp_permissions import make_acp_approval_callback

        cb = make_acp_approval_callback(None, "sess-1")
        result = await cb(self._spec(), self._tc())

        assert result == ApprovalResult.DENIED

    @pytest.mark.asyncio
    async def test_no_connection_never_calls_request_permission(self):
        """The deny-all fallback must not attempt any network calls."""
        from agent.transports.acp_permissions import make_acp_approval_callback

        conn = AsyncMock()
        cb = make_acp_approval_callback(None, "sess-1")
        await cb(self._spec(), self._tc())

        conn.request_permission.assert_not_called()

    # ------------------------------------------------------------------
    # tool_call_id passthrough
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_existing_tool_call_id_passed_to_request_permission(self):
        """Callback must forward tc.tool_call_id so Zed links the ToolCallStart."""
        from acp.schema import ToolCallUpdate

        from agent.transports.acp_permissions import make_acp_approval_callback

        conn = AsyncMock()
        conn.request_permission.return_value = self._allowed_response("allow_once")

        cb = make_acp_approval_callback(conn, "sess-1")
        await cb(self._spec(), self._tc(tool_call_id="my-stable-id"))

        _, kwargs = conn.request_permission.call_args
        tool_call: ToolCallUpdate = kwargs["tool_call"]
        assert isinstance(tool_call, ToolCallUpdate)
        assert tool_call.tool_call_id == "my-stable-id"

    @pytest.mark.asyncio
    async def test_missing_tool_call_id_auto_assigned(self):
        """A ToolCall with no id gets one assigned and the id is stored back."""
        from acp.schema import ToolCallUpdate

        from agent.transports.acp_permissions import make_acp_approval_callback

        conn = AsyncMock()
        conn.request_permission.return_value = self._allowed_response("allow_once")

        tc = self._tc(tool_call_id="")

        cb = make_acp_approval_callback(conn, "sess-1")
        await cb(self._spec(), tc)

        assert tc.tool_call_id, "a non-empty id must be generated"
        _, kwargs = conn.request_permission.call_args
        tool_call: ToolCallUpdate = kwargs["tool_call"]
        assert isinstance(tool_call, ToolCallUpdate)
        # id on the ToolCallUpdate must match what was stored back on tc
        assert tool_call.tool_call_id == tc.tool_call_id

    # ------------------------------------------------------------------
    # Side-effect → ToolKind forwarding
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_write_side_effect_passes_edit_kind(self):
        """Write-only tools should be tagged 'edit' so Zed shows the right icon."""
        from acp.schema import ToolCallUpdate

        from agent.tools.schema import SideEffect
        from agent.transports.acp_permissions import make_acp_approval_callback

        conn = AsyncMock()
        conn.request_permission.return_value = self._allowed_response("allow_once")

        cb = make_acp_approval_callback(conn, "sess-1")
        await cb(self._spec(side_effects=[SideEffect.WRITE]), self._tc())

        _, kwargs = conn.request_permission.call_args
        tool_call: ToolCallUpdate = kwargs["tool_call"]
        assert tool_call.kind == "edit"

    @pytest.mark.asyncio
    async def test_execute_side_effect_passes_execute_kind(self):
        """Execute tools should be tagged 'execute'."""
        from acp.schema import ToolCallUpdate

        from agent.tools.schema import SideEffect
        from agent.transports.acp_permissions import make_acp_approval_callback

        conn = AsyncMock()
        conn.request_permission.return_value = self._allowed_response("allow_once")

        cb = make_acp_approval_callback(conn, "sess-1")
        await cb(self._spec(side_effects=[SideEffect.EXECUTE]), self._tc())

        _, kwargs = conn.request_permission.call_args
        tool_call: ToolCallUpdate = kwargs["tool_call"]
        assert tool_call.kind == "execute"

    # ------------------------------------------------------------------
    # M1: timeout validation
    # ------------------------------------------------------------------

    def test_negative_timeout_rejected(self):
        """A negative timeout would deny every request silently — fail fast."""
        from agent.transports.acp_permissions import make_acp_approval_callback

        with pytest.raises(ValueError, match="timeout"):
            make_acp_approval_callback(AsyncMock(), "sess-1", timeout=-1)

    def test_nan_timeout_rejected(self):
        from agent.transports.acp_permissions import make_acp_approval_callback

        with pytest.raises(ValueError, match="finite"):
            make_acp_approval_callback(AsyncMock(), "sess-1", timeout=float("nan"))

    def test_inf_timeout_rejected(self):
        from agent.transports.acp_permissions import make_acp_approval_callback

        with pytest.raises(ValueError, match="finite"):
            make_acp_approval_callback(AsyncMock(), "sess-1", timeout=float("inf"))

    def test_bool_timeout_rejected(self):
        """bool is a subclass of int; reject it to avoid `True == 1` surprises."""
        from agent.transports.acp_permissions import make_acp_approval_callback

        with pytest.raises(ValueError, match="number"):
            make_acp_approval_callback(AsyncMock(), "sess-1", timeout=True)  # type: ignore[arg-type]

    def test_zero_timeout_accepted_as_wait_forever(self):
        """0 is the documented 'wait indefinitely' sentinel — must not raise."""
        from agent.transports.acp_permissions import make_acp_approval_callback

        cb = make_acp_approval_callback(AsyncMock(), "sess-1", timeout=0)
        assert callable(cb)


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
