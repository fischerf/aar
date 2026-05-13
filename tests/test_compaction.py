"""Tests for agent.core.compaction — token estimation, cut points, serialization, and session-level compaction."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.core.compaction.compaction import (
    CompactionResult,
    estimate_context_tokens,
    estimate_event_tokens,
    estimate_message_tokens,
    find_event_cut_point,
    generate_summary,
    should_compact,
)
from agent.core.compaction.utils import (
    FileOperations,
    compute_file_lists,
    create_file_ops,
    extract_file_ops_from_message,
    format_file_operations,
    serialize_conversation,
)
from agent.core.config import CompactionConfig
from agent.core.events import (
    AssistantMessage,
    ErrorEvent,
    ToolCall,
    ToolResult,
    UserMessage,
)
from agent.core.session import Session, events_to_messages

# ── Helpers ──────────────────────────────────────────────────────────────


def _user_msg(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def _assistant_msg(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text}


def _assistant_tool_msg(text: str, tool_name: str, tool_id: str, args: dict) -> dict[str, Any]:
    blocks: list[dict] = []
    if text:
        blocks.append({"type": "text", "text": text})
    blocks.append({"type": "tool_use", "id": tool_id, "name": tool_name, "input": args})
    return {"role": "assistant", "content": blocks}


def _tool_result_msg(tool_id: str, output: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": output, "is_error": False}
        ],
    }


# ── Token Estimation ─────────────────────────────────────────────────────


class TestEstimateMessageTokens:
    def test_simple_string_content(self):
        msg = _user_msg("a" * 400)
        assert estimate_message_tokens(msg) == 100

    def test_empty_content(self):
        msg = _user_msg("")
        assert estimate_message_tokens(msg) == 1  # minimum 1

    def test_assistant_with_tool_use(self):
        msg = _assistant_tool_msg("ok", "read_file", "t1", {"path": "foo.py"})
        tokens = estimate_message_tokens(msg)
        assert tokens > 0

    def test_tool_result_content(self):
        msg = _tool_result_msg("t1", "x" * 800)
        tokens = estimate_message_tokens(msg)
        assert tokens == 200


class TestEstimateEventTokens:
    def test_user_message(self):
        event = UserMessage(content="a" * 400)
        assert estimate_event_tokens(event) == 100

    def test_assistant_message(self):
        event = AssistantMessage(content="b" * 200)
        assert estimate_event_tokens(event) == 50

    def test_tool_call(self):
        event = ToolCall(tool_name="read_file", tool_call_id="t1", arguments={"path": "x.py"})
        tokens = estimate_event_tokens(event)
        assert tokens > 0

    def test_tool_result(self):
        event = ToolResult(tool_call_id="t1", tool_name="read_file", output="c" * 100)
        assert estimate_event_tokens(event) == 25

    def test_unhandled_event_returns_zero(self):
        event = ErrorEvent(message="boom")
        assert estimate_event_tokens(event) == 0


class TestEstimateContextTokens:
    def test_falls_back_to_heuristic(self):
        msgs = [_user_msg("a" * 400), _assistant_msg("b" * 200)]
        tokens = estimate_context_tokens(msgs)
        assert tokens == 150

    def test_uses_provider_usage_when_available(self):
        msgs = [_user_msg("a" * 400)]
        usage = {"input_tokens": 5000, "output_tokens": 1000}
        tokens = estimate_context_tokens(msgs, last_usage=usage)
        assert tokens == 6000

    def test_zero_usage_falls_back(self):
        msgs = [_user_msg("a" * 400)]
        usage = {"input_tokens": 0, "output_tokens": 0}
        tokens = estimate_context_tokens(msgs, last_usage=usage)
        assert tokens == 100  # chars / 4 heuristic


# ── Compaction Trigger ───────────────────────────────────────────────────


class TestShouldCompact:
    def test_triggers_when_over_threshold(self):
        assert should_compact(context_tokens=90_000, context_window=100_000, reserve_tokens=16_000)

    def test_no_trigger_when_under_threshold(self):
        assert not should_compact(
            context_tokens=50_000, context_window=100_000, reserve_tokens=16_000
        )

    def test_no_trigger_when_window_zero(self):
        assert not should_compact(context_tokens=90_000, context_window=0, reserve_tokens=16_000)

    def test_exact_threshold(self):
        # tokens == window - reserve → not over, should NOT compact
        assert not should_compact(
            context_tokens=84_000, context_window=100_000, reserve_tokens=16_000
        )

    def test_one_over_threshold(self):
        assert should_compact(context_tokens=84_001, context_window=100_000, reserve_tokens=16_000)


# ── Cut-point Detection ─────────────────────────────────────────────────


class TestFindEventCutPoint:
    def test_short_conversation_returns_zero(self):
        events = [UserMessage(content="hi"), AssistantMessage(content="hello")]
        assert find_event_cut_point(events, keep_recent_tokens=100) == 0

    def test_keeps_recent_tokens(self):
        # Build a conversation where each user message is ~100 tokens (400 chars)
        events = []
        for i in range(10):
            events.append(UserMessage(content=f"msg{i} " + "x" * 396))
            events.append(AssistantMessage(content=f"reply{i} " + "y" * 396))

        # keep_recent_tokens=200 should keep ~2 messages (200 tokens ≈ 800 chars)
        cut = find_event_cut_point(events, keep_recent_tokens=200)
        assert cut > 0
        assert cut < len(events)
        # Cut must be at a UserMessage
        assert isinstance(events[cut], UserMessage)

    def test_returns_zero_when_all_fits(self):
        events = [
            UserMessage(content="hello"),
            AssistantMessage(content="hi"),
        ]
        assert find_event_cut_point(events, keep_recent_tokens=100_000) == 0

    def test_cuts_at_user_message_boundary(self):
        events = [
            UserMessage(content="a" * 400),  # 100 tokens
            ToolCall(tool_name="read_file", tool_call_id="t1", arguments={"path": "x.py"}),
            AssistantMessage(content="b" * 400),  # 100 tokens
            ToolResult(tool_call_id="t1", tool_name="read_file", output="c" * 400),
            UserMessage(content="d" * 400),  # 100 tokens — this should be the cut
            AssistantMessage(content="e" * 400),  # 100 tokens
        ]
        cut = find_event_cut_point(events, keep_recent_tokens=200)
        assert isinstance(events[cut], UserMessage)


# ── File Operations ──────────────────────────────────────────────────────


class TestFileOperations:
    def test_create_file_ops(self):
        ops = create_file_ops()
        assert isinstance(ops, FileOperations)
        assert len(ops.read) == 0

    def test_extract_from_assistant_message(self):
        msg = _assistant_tool_msg("", "read_file", "t1", {"path": "src/main.py"})
        ops = create_file_ops()
        extract_file_ops_from_message(msg, ops)
        assert "src/main.py" in ops.read

    def test_extract_write(self):
        msg = _assistant_tool_msg("", "write_file", "t2", {"path": "out.txt"})
        ops = create_file_ops()
        extract_file_ops_from_message(msg, ops)
        assert "out.txt" in ops.written

    def test_extract_edit(self):
        msg = _assistant_tool_msg("", "edit_file", "t3", {"path": "src/lib.py"})
        ops = create_file_ops()
        extract_file_ops_from_message(msg, ops)
        assert "src/lib.py" in ops.edited

    def test_ignores_user_messages(self):
        msg = _user_msg("read_file foo.py")
        ops = create_file_ops()
        extract_file_ops_from_message(msg, ops)
        assert len(ops.read) == 0

    def test_compute_file_lists_deduplicates(self):
        ops = FileOperations(
            read={"a.py", "b.py", "c.py"},
            written={"b.py"},
            edited={"c.py"},
        )
        read_only, modified = compute_file_lists(ops)
        assert read_only == ["a.py"]
        assert modified == ["b.py", "c.py"]

    def test_format_file_operations_empty(self):
        assert format_file_operations([], []) == ""

    def test_format_file_operations_with_files(self):
        result = format_file_operations(["a.py"], ["b.py"])
        assert "<read-files>" in result
        assert "a.py" in result
        assert "<modified-files>" in result
        assert "b.py" in result


# ── Serialization ────────────────────────────────────────────────────────


class TestSerializeConversation:
    def test_user_and_assistant(self):
        msgs = [_user_msg("hello"), _assistant_msg("hi there")]
        text = serialize_conversation(msgs)
        assert "[User]: hello" in text
        assert "[Assistant]: hi there" in text

    def test_tool_calls_serialized(self):
        msgs = [
            _assistant_tool_msg("thinking", "read_file", "t1", {"path": "foo.py"}),
        ]
        text = serialize_conversation(msgs)
        assert "[Assistant]: thinking" in text
        assert "[Assistant tool calls]: read_file(" in text

    def test_tool_results_serialized(self):
        msgs = [_tool_result_msg("t1", "file content here")]
        text = serialize_conversation(msgs)
        assert "[Tool result (t1)]: file content here" in text

    def test_empty_messages(self):
        assert serialize_conversation([]) == ""


# ── events_to_messages ───────────────────────────────────────────────────


class TestEventsToMessages:
    def test_roundtrip_matches_session(self):
        """Standalone function produces same output as Session.to_messages()."""
        session = Session()
        session.add_user_message("hello")
        session.add_tool_call(tool_name="read_file", tool_call_id="t1", arguments={"path": "x"})
        session.add_assistant_message("done", stop_reason="end_turn")
        session.add_tool_result(tool_call_id="t1", tool_name="read_file", output="content")
        session.add_user_message("next")

        from_session = session.to_messages()
        from_standalone = events_to_messages(session.events)
        assert from_session == from_standalone

    def test_subset_of_events(self):
        """Can convert a subset of events without a full Session."""
        events = [
            UserMessage(content="hello"),
            AssistantMessage(content="hi"),
        ]
        msgs = events_to_messages(events)
        assert len(msgs) == 2
        assert msgs[0] == {"role": "user", "content": "hello"}
        assert msgs[1] == {"role": "assistant", "content": "hi"}


# ── Session.apply_compaction ─────────────────────────────────────────────


class TestApplyCompaction:
    def test_replaces_old_events_with_summary(self):
        session = Session()
        session.add_user_message("msg1")
        session.add_assistant_message("reply1")
        session.add_user_message("msg2")
        session.add_assistant_message("reply2")
        session.add_user_message("msg3")
        session.add_assistant_message("reply3")

        assert len(session.events) == 6

        session.apply_compaction(4, "[Summary of earlier conversation]")

        assert len(session.events) == 3  # summary + msg3 + reply3
        assert isinstance(session.events[0], UserMessage)
        assert "[Summary" in session.events[0].content
        assert session.events[1].content == "msg3"
        assert session.events[2].content == "reply3"

    def test_to_messages_after_compaction(self):
        session = Session()
        session.add_user_message("old1")
        session.add_assistant_message("old2")
        session.add_user_message("recent")
        session.add_assistant_message("reply")

        session.apply_compaction(2, "Summary of old conversation")

        msgs = session.to_messages()
        assert len(msgs) == 3  # summary + recent + reply
        assert msgs[0]["role"] == "user"
        assert "Summary" in msgs[0]["content"]
        assert msgs[1]["content"] == "recent"


# ── generate_summary ─────────────────────────────────────────────────────


class TestGenerateSummary:
    @pytest.mark.asyncio
    async def test_calls_provider_with_conversation(self):
        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(
            return_value=MagicMock(content="## Goal\nBuild a widget")
        )

        msgs = [_user_msg("Build me a widget"), _assistant_msg("I'll build that")]
        result = await generate_summary(msgs, mock_provider)

        assert "## Goal" in result
        mock_provider.complete.assert_called_once()

        # Check the call included serialized conversation
        call_args = mock_provider.complete.call_args
        messages_arg = call_args.kwargs.get("messages") or call_args[1].get("messages")
        assert len(messages_arg) == 1
        assert "<conversation>" in messages_arg[0]["content"]

    @pytest.mark.asyncio
    async def test_uses_update_prompt_with_previous_summary(self):
        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(
            return_value=MagicMock(content="## Goal\nUpdated summary")
        )

        msgs = [_user_msg("next step")]
        result = await generate_summary(msgs, mock_provider, previous_summary="## Goal\nOld goal")

        assert "Updated summary" in result
        call_args = mock_provider.complete.call_args
        messages_arg = call_args.kwargs.get("messages") or call_args[1].get("messages")
        assert "<previous-summary>" in messages_arg[0]["content"]


# ── compact_session ──────────────────────────────────────────────────────


class TestCompactSession:
    @pytest.mark.asyncio
    async def test_no_compaction_when_under_threshold(self):
        from agent.core.compaction.compaction import compact_session

        session = Session()
        session.add_user_message("hi")
        session.add_assistant_message("hello")

        config = CompactionConfig(enabled=True, reserve_tokens=100, keep_recent_tokens=100)
        mock_provider = AsyncMock()

        result = await compact_session(
            session, mock_provider, context_window=100_000, config=config
        )
        assert result is None
        mock_provider.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_compacts_when_over_threshold(self):
        from agent.core.compaction.compaction import compact_session

        session = Session()
        # Create enough content to exceed the threshold
        for i in range(20):
            session.add_user_message(f"message {i} " + "x" * 2000)
            session.add_assistant_message(f"reply {i} " + "y" * 2000)

        original_event_count = len(session.events)
        assert original_event_count == 40

        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(
            return_value=MagicMock(content="## Goal\nSummary of conversation")
        )

        # Small window forces compaction
        config = CompactionConfig(enabled=True, reserve_tokens=500, keep_recent_tokens=2000)
        result = await compact_session(session, mock_provider, context_window=5000, config=config)

        assert result is not None
        assert isinstance(result, CompactionResult)
        assert result.events_removed > 0
        assert len(session.events) < original_event_count
        assert "Summary of conversation" in result.summary
        # Session metadata stores the summary for iterative updates
        assert "compaction_summary" in session.metadata

    @pytest.mark.asyncio
    async def test_compacted_session_messages_are_valid(self):
        from agent.core.compaction.compaction import compact_session

        session = Session()
        for i in range(10):
            session.add_user_message(f"msg{i} " + "x" * 1000)
            session.add_assistant_message(f"reply{i} " + "y" * 1000)

        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(return_value=MagicMock(content="Compacted summary"))

        config = CompactionConfig(enabled=True, reserve_tokens=200, keep_recent_tokens=500)
        await compact_session(session, mock_provider, context_window=2000, config=config)

        # Messages should be valid after compaction
        msgs = session.to_messages()
        assert len(msgs) > 0
        # First message should be the summary
        assert msgs[0]["role"] == "user"
        assert "Compacted summary" in msgs[0]["content"]


# ── CompactionConfig ─────────────────────────────────────────────────────


class TestCompactionConfig:
    def test_defaults(self):
        cfg = CompactionConfig()
        assert cfg.enabled is False
        assert cfg.reserve_tokens == 16_384
        assert cfg.keep_recent_tokens == 20_000

    def test_from_dict(self):
        cfg = CompactionConfig(enabled=True, reserve_tokens=8000, keep_recent_tokens=10_000)
        assert cfg.enabled is True
        assert cfg.reserve_tokens == 8000

    def test_agent_config_includes_compaction(self):
        from agent.core.config import AgentConfig

        config = AgentConfig()
        assert hasattr(config, "compaction")
        assert isinstance(config.compaction, CompactionConfig)
        assert config.compaction.enabled is False

    def test_agent_config_serialization(self):
        from agent.core.config import AgentConfig

        config = AgentConfig(compaction=CompactionConfig(enabled=True, reserve_tokens=8000))
        data = config.model_dump()
        assert data["compaction"]["enabled"] is True
        assert data["compaction"]["reserve_tokens"] == 8000

        # Round-trip
        config2 = AgentConfig.model_validate(data)
        assert config2.compaction.enabled is True
