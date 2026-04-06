"""Event model tests — serialization, deserialization, all event types."""

from __future__ import annotations

import pytest

from agent.core.events import (
    EVENT_TYPE_MAP,
    AssistantMessage,
    ContentBlock,
    ErrorEvent,
    Event,
    EventType,
    ImageURL,
    ImageURLBlock,
    ProviderMeta,
    ReasoningBlock,
    SessionEvent,
    StopReason,
    TextBlock,
    ToolCall,
    ToolResult,
    UserMessage,
    deserialize_event,
)

# ---------------------------------------------------------------------------
# Content block models (multimodal)
# ---------------------------------------------------------------------------


class TestContentBlocks:
    def test_text_block_defaults(self):
        b = TextBlock(text="hello")
        assert b.type == "text"
        assert b.text == "hello"

    def test_image_url_block_http(self):
        b = ImageURLBlock(image_url=ImageURL(url="https://example.com/img.png"))
        assert b.type == "image_url"
        assert b.image_url.url == "https://example.com/img.png"
        assert b.image_url.detail is None

    def test_image_url_block_data_uri(self):
        data_uri = "data:image/jpeg;base64,/9j/4AAQ"
        b = ImageURLBlock(image_url=ImageURL(url=data_uri, detail="high"))
        assert b.image_url.url == data_uri
        assert b.image_url.detail == "high"

    def test_image_url_block_model_dump_excludes_none(self):
        b = ImageURLBlock(image_url=ImageURL(url="https://example.com/img.png"))
        d = b.model_dump(exclude_none=True)
        assert d == {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}}
        assert "detail" not in d["image_url"]

    def test_content_block_discriminated_union_text(self):
        from pydantic import TypeAdapter

        ta: TypeAdapter[ContentBlock] = TypeAdapter(ContentBlock)
        block = ta.validate_python({"type": "text", "text": "hi"})
        assert isinstance(block, TextBlock)

    def test_content_block_discriminated_union_image(self):
        from pydantic import TypeAdapter

        ta: TypeAdapter[ContentBlock] = TypeAdapter(ContentBlock)
        block = ta.validate_python(
            {"type": "image_url", "image_url": {"url": "https://example.com/a.jpg"}}
        )
        assert isinstance(block, ImageURLBlock)


class TestUserMessageMultimodal:
    def test_text_only_not_multimodal(self):
        msg = UserMessage(content="hello")
        assert not msg.is_multimodal
        assert msg.parts == []

    def test_parts_set_is_multimodal(self):
        parts: list[ContentBlock] = [
            TextBlock(text="what is this?"),
            ImageURLBlock(image_url=ImageURL(url="https://example.com/img.png")),
        ]
        msg = UserMessage(content="what is this?", parts=parts)
        assert msg.is_multimodal

    def test_parts_text_only_not_multimodal(self):
        # parts populated with only TextBlocks still counts as multimodal
        # (the flag is purely structural)
        msg = UserMessage(content="hi", parts=[TextBlock(text="hi")])
        assert msg.is_multimodal

    def test_parts_serialization(self):
        parts: list[ContentBlock] = [
            TextBlock(text="describe"),
            ImageURLBlock(image_url=ImageURL(url="https://example.com/img.png")),
        ]
        msg = UserMessage(content="describe", parts=parts)
        dumped = [p.model_dump(exclude_none=True) for p in msg.parts]
        assert dumped[0] == {"type": "text", "text": "describe"}
        assert dumped[1] == {
            "type": "image_url",
            "image_url": {"url": "https://example.com/img.png"},
        }


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestEventConstruction:
    def test_user_message(self):
        msg = UserMessage(content="hello")
        assert msg.type == EventType.USER_MESSAGE
        assert msg.content == "hello"
        assert msg.id  # auto-generated
        assert msg.timestamp > 0

    def test_assistant_message_with_stop_reason(self):
        msg = AssistantMessage(content="hi", stop_reason=StopReason.END_TURN)
        assert msg.type == EventType.ASSISTANT_MESSAGE
        assert msg.stop_reason == StopReason.END_TURN

    def test_assistant_message_no_stop_reason(self):
        msg = AssistantMessage(content="hi")
        assert msg.stop_reason is None

    def test_tool_call(self):
        tc = ToolCall(tool_name="read_file", tool_call_id="tc_1", arguments={"path": "test.py"})
        assert tc.type == EventType.TOOL_CALL
        assert tc.tool_name == "read_file"
        assert tc.arguments == {"path": "test.py"}

    def test_tool_result(self):
        tr = ToolResult(
            tool_call_id="tc_1", tool_name="read_file", output="contents", is_error=False
        )
        assert tr.type == EventType.TOOL_RESULT
        assert not tr.is_error

    def test_tool_result_error(self):
        tr = ToolResult(
            tool_call_id="tc_1", tool_name="bash", output="Error: timeout", is_error=True
        )
        assert tr.is_error

    def test_reasoning_block(self):
        rb = ReasoningBlock(content="Let me think...")
        assert rb.type == EventType.REASONING

    def test_provider_meta(self):
        meta = ProviderMeta(
            provider="anthropic",
            model="claude-3",
            usage={"input_tokens": 100, "output_tokens": 50},
            request_id="req_123",
        )
        assert meta.type == EventType.PROVIDER_META
        assert meta.usage["input_tokens"] == 100

    def test_error_event(self):
        err = ErrorEvent(message="something broke", recoverable=False)
        assert err.type == EventType.ERROR
        assert not err.recoverable

    def test_session_event(self):
        se = SessionEvent(action="started")
        assert se.type == EventType.SESSION
        assert se.action == "started"

    def test_unique_ids(self):
        e1 = UserMessage(content="a")
        e2 = UserMessage(content="b")
        assert e1.id != e2.id


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------


class TestEventSerialization:
    @pytest.mark.parametrize(
        "event",
        [
            UserMessage(content="hello world"),
            AssistantMessage(content="response", stop_reason=StopReason.END_TURN),
            ToolCall(tool_name="bash", tool_call_id="tc_42", arguments={"command": "ls"}),
            ToolResult(tool_call_id="tc_42", tool_name="bash", output="file.txt", is_error=False),
            ReasoningBlock(content="thinking..."),
            ProviderMeta(
                provider="openai", model="gpt-4o", usage={"input_tokens": 1}, request_id="r1"
            ),
            ErrorEvent(message="boom", recoverable=True),
            SessionEvent(action="ended"),
        ],
    )
    def test_round_trip(self, event: Event):
        """Every event type should survive serialize → deserialize."""
        data = event.model_dump()
        restored = deserialize_event(data)
        assert type(restored) is type(event)
        assert restored.type == event.type
        assert restored.id == event.id

    def test_user_message_round_trip_content(self):
        original = UserMessage(content="specific content 🚀")
        restored = deserialize_event(original.model_dump())
        assert restored.content == "specific content 🚀"

    def test_tool_call_round_trip_arguments(self):
        original = ToolCall(
            tool_name="write_file",
            tool_call_id="tc_99",
            arguments={"path": "/tmp/test.py", "content": "print('hi')"},
        )
        restored = deserialize_event(original.model_dump())
        assert restored.arguments == original.arguments

    def test_json_serialization(self):
        msg = UserMessage(content="json test")
        json_str = msg.model_dump_json()
        assert '"user_message"' in json_str
        assert '"json test"' in json_str


# ---------------------------------------------------------------------------
# Event type map coverage
# ---------------------------------------------------------------------------


class TestEventTypeMap:
    def test_all_event_types_have_mapping(self):
        """Every EventType should have a corresponding class in EVENT_TYPE_MAP."""
        for et in EventType:
            assert et in EVENT_TYPE_MAP, f"Missing mapping for {et}"

    def test_deserialization_with_invalid_type_raises(self):
        with pytest.raises((ValueError, KeyError)):
            deserialize_event({"type": "nonexistent_type"})


# ---------------------------------------------------------------------------
# Stop reason enum
# ---------------------------------------------------------------------------


class TestStopReason:
    def test_all_values(self):
        expected = {
            "end_turn",
            "tool_use",
            "max_tokens",
            "timeout",
            "max_steps",
            "cancelled",
            "error",
        }
        actual = {s.value for s in StopReason}
        assert actual == expected

    def test_string_comparison(self):
        assert StopReason.END_TURN == "end_turn"
        assert StopReason.TOOL_USE.value == "tool_use"
