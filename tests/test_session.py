"""Session tests — persistence, resumption, message conversion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.core.events import (
    AssistantMessage,
    EventType,
    StopReason,
    ToolCall,
    ToolResult,
    UserMessage,
    deserialize_event,
)
from agent.core.session import Session
from agent.core.state import AgentState
from agent.memory.session_store import SessionStore


# ---------------------------------------------------------------------------
# Session basics
# ---------------------------------------------------------------------------


class TestSessionBasics:
    def test_new_session_has_id(self):
        s = Session()
        assert s.session_id
        assert s.run_id
        assert s.trace_id
        assert s.state == AgentState.IDLE
        assert s.step_count == 0
        assert s.events == []

    def test_session_ids_are_unique(self):
        s1, s2 = Session(), Session()
        assert s1.session_id != s2.session_id
        assert s1.trace_id != s2.trace_id

    def test_add_user_message(self):
        s = Session()
        msg = s.add_user_message("hello")
        assert len(s.events) == 1
        assert isinstance(msg, UserMessage)
        assert msg.content == "hello"

    def test_add_assistant_message(self):
        s = Session()
        msg = s.add_assistant_message("hi", stop_reason=StopReason.END_TURN)
        assert len(s.events) == 1
        assert isinstance(msg, AssistantMessage)
        assert msg.stop_reason == StopReason.END_TURN

    def test_add_tool_call_and_result(self):
        s = Session()
        tc = s.add_tool_call(tool_name="echo", tool_call_id="tc_1", arguments={"msg": "hi"})
        tr = s.add_tool_result(tool_call_id="tc_1", tool_name="echo", output="echo: hi")
        assert len(s.events) == 2
        assert isinstance(tc, ToolCall)
        assert isinstance(tr, ToolResult)

    def test_append_list(self):
        s = Session()
        msgs = [UserMessage(content="a"), UserMessage(content="b")]
        s.append(msgs)
        assert len(s.events) == 2

    def test_increment_step(self):
        s = Session()
        assert s.increment_step() == 1
        assert s.increment_step() == 2
        assert s.step_count == 2


# ---------------------------------------------------------------------------
# Message conversion (to_messages)
# ---------------------------------------------------------------------------


class TestToMessages:
    def test_simple_conversation(self):
        s = Session()
        s.add_user_message("Hello")
        s.add_assistant_message("Hi there")
        msgs = s.to_messages()
        assert len(msgs) == 2
        assert msgs[0] == {"role": "user", "content": "Hello"}
        assert msgs[1] == {"role": "assistant", "content": "Hi there"}

    def test_tool_call_message_structure(self):
        """Tool calls should be grouped with the assistant message.

        The loop emits events in this order:
        ToolCall → AssistantMessage(TOOL_USE) → ToolResult → AssistantMessage(final)
        In to_messages(), pending tool calls are merged into the AssistantMessage
        that immediately follows them.
        """
        s = Session()
        s.add_user_message("Read test.py")
        # The loop emits: tool calls FIRST, then assistant msg, then tool results
        s.add_tool_call(tool_name="read_file", tool_call_id="tc_1", arguments={"path": "test.py"})
        s.add_assistant_message("Let me read that", stop_reason=StopReason.TOOL_USE)
        s.add_tool_result(tool_call_id="tc_1", tool_name="read_file", output="print('hi')")
        s.add_assistant_message("The file contains a print statement")

        msgs = s.to_messages()
        assert len(msgs) == 4
        # msg[0]: user
        assert msgs[0]["role"] == "user"
        # msg[1]: assistant with tool_use block merged in
        assert msgs[1]["role"] == "assistant"
        assert isinstance(msgs[1]["content"], list)
        tool_use_blocks = [b for b in msgs[1]["content"] if b.get("type") == "tool_use"]
        assert len(tool_use_blocks) == 1
        assert tool_use_blocks[0]["name"] == "read_file"
        # msg[2]: tool result as user message
        assert msgs[2]["role"] == "user"
        assert msgs[2]["content"][0]["type"] == "tool_result"
        # msg[3]: final assistant answer
        assert msgs[3]["role"] == "assistant"
        assert msgs[3]["content"] == "The file contains a print statement"

    def test_multiple_tool_calls_grouped(self):
        """Multiple tool calls should all appear in the assistant message."""
        s = Session()
        s.add_user_message("Do two things")
        s.append(ToolCall(tool_name="echo", tool_call_id="tc_1", arguments={"message": "a"}))
        s.append(ToolCall(tool_name="echo", tool_call_id="tc_2", arguments={"message": "b"}))
        s.append(ToolResult(tool_call_id="tc_1", tool_name="echo", output="echo: a"))
        s.append(ToolResult(tool_call_id="tc_2", tool_name="echo", output="echo: b"))

        msgs = s.to_messages()
        # user, assistant (with 2 tool_use blocks), user (with 2 tool_result blocks)
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"
        assert len(msgs[1]["content"]) == 2  # two tool_use blocks
        assert msgs[2]["role"] == "user"
        assert len(msgs[2]["content"]) == 2  # two tool_result blocks

    def test_empty_session_returns_empty(self):
        s = Session()
        assert s.to_messages() == []


# ---------------------------------------------------------------------------
# Session persistence (JSONL)
# ---------------------------------------------------------------------------


class TestSessionPersistence:
    def test_save_and_load(self, tmp_dir: Path):
        store = SessionStore(tmp_dir)
        s = Session()
        s.add_user_message("persist me")
        s.add_assistant_message("persisted")
        s.state = AgentState.COMPLETED
        s.step_count = 5

        store.save(s)
        loaded = store.load(s.session_id)

        assert loaded.session_id == s.session_id
        assert loaded.state == AgentState.COMPLETED
        assert loaded.step_count == 5
        assert len(loaded.events) == 2
        assert loaded.events[0].content == "persist me"
        assert loaded.events[1].content == "persisted"

    def test_load_nonexistent_raises(self, tmp_dir: Path):
        store = SessionStore(tmp_dir)
        with pytest.raises(FileNotFoundError):
            store.load("nonexistent_session_id")

    def test_list_sessions(self, tmp_dir: Path):
        store = SessionStore(tmp_dir)
        s1 = Session()
        s2 = Session()
        store.save(s1)
        store.save(s2)

        ids = store.list_sessions()
        assert s1.session_id in ids
        assert s2.session_id in ids

    def test_delete_session(self, tmp_dir: Path):
        store = SessionStore(tmp_dir)
        s = Session()
        store.save(s)
        assert store.delete(s.session_id)
        assert s.session_id not in store.list_sessions()
        assert not store.delete("already_deleted")

    def test_save_preserves_all_event_types(self, tmp_dir: Path):
        """All event types should survive persistence."""
        store = SessionStore(tmp_dir)
        s = Session()
        s.add_user_message("hello")
        s.add_assistant_message("hi", stop_reason=StopReason.END_TURN)
        s.add_tool_call(tool_name="bash", tool_call_id="tc_1", arguments={"command": "ls"})
        s.add_tool_result(tool_call_id="tc_1", tool_name="bash", output="file.txt")

        store.save(s)
        loaded = store.load(s.session_id)

        assert len(loaded.events) == 4
        assert isinstance(loaded.events[0], UserMessage)
        assert isinstance(loaded.events[1], AssistantMessage)
        assert isinstance(loaded.events[2], ToolCall)
        assert isinstance(loaded.events[3], ToolResult)

    def test_jsonl_format(self, tmp_dir: Path):
        """The file should be valid JSONL with a header line."""
        store = SessionStore(tmp_dir)
        s = Session()
        s.add_user_message("test")
        path = store.save(s)

        lines = path.read_text().strip().split("\n")
        assert len(lines) == 2  # header + 1 event

        header = json.loads(lines[0])
        assert header["_meta"] is True
        assert header["session_id"] == s.session_id
        assert header["trace_id"] == s.trace_id

        event = json.loads(lines[1])
        assert event["type"] == "user_message"

    def test_save_and_load_preserves_trace_id(self, tmp_dir: Path):
        store = SessionStore(tmp_dir)
        s = Session()
        s.add_user_message("hi")
        store.save(s)

        loaded = store.load(s.session_id)
        assert loaded.trace_id == s.trace_id

    def test_compact_truncates_old_events(self, tmp_dir: Path):
        store = SessionStore(tmp_dir)
        s = Session()
        for i in range(10):
            s.add_user_message(f"msg {i}")
        store.save(s)

        compacted = store.compact(s.session_id, max_events=5)
        assert len(compacted.events) == 5
        assert compacted.events[0].content == "msg 5"  # oldest 5 pruned

    def test_compact_no_op_when_under_limit(self, tmp_dir: Path):
        store = SessionStore(tmp_dir)
        s = Session()
        s.add_user_message("only one")
        store.save(s)

        compacted = store.compact(s.session_id, max_events=100)
        assert len(compacted.events) == 1


# ---------------------------------------------------------------------------
# Session resumption
# ---------------------------------------------------------------------------


class TestSessionResumption:
    def test_resume_and_continue(self, tmp_dir: Path):
        """A loaded session should be usable for continued conversation."""
        store = SessionStore(tmp_dir)

        # First conversation
        s = Session()
        s.add_user_message("What is 2+2?")
        s.add_assistant_message("4")
        s.step_count = 1
        store.save(s)

        # Resume
        loaded = store.load(s.session_id)
        loaded.add_user_message("And 3+3?")
        loaded.add_assistant_message("6")
        loaded.step_count = 2
        store.save(loaded)

        # Verify full history
        final = store.load(s.session_id)
        assert len(final.events) == 4
        assert final.step_count == 2

    def test_resumed_session_to_messages(self, tmp_dir: Path):
        """Message conversion should work correctly after resumption."""
        store = SessionStore(tmp_dir)
        s = Session()
        s.add_user_message("First")
        s.add_assistant_message("Reply")
        store.save(s)

        loaded = store.load(s.session_id)
        loaded.add_user_message("Second")

        msgs = loaded.to_messages()
        assert len(msgs) == 3
        assert msgs[0]["content"] == "First"
        assert msgs[1]["content"] == "Reply"
        assert msgs[2]["content"] == "Second"
