"""Typed internal event model for the agent runtime."""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Content block models (for multimodal / image input)
# ---------------------------------------------------------------------------


class TextBlock(BaseModel):
    """A plain-text content block in a multimodal user message."""

    type: Literal["text"] = "text"
    text: str


class ImageURL(BaseModel):
    """Points to an image by HTTP/HTTPS URL or a ``data:`` URI."""

    url: str
    detail: str | None = None  # OpenAI vision detail hint ("auto" | "low" | "high")


class ImageURLBlock(BaseModel):
    """An image content block, identified by URL or base-64 data URI."""

    type: Literal["image_url"] = "image_url"
    image_url: ImageURL


# Pydantic v2 discriminated union dispatched on the ``type`` field.
ContentBlock = Annotated[TextBlock | ImageURLBlock, Field(discriminator="type")]


# ---------------------------------------------------------------------------
# Core event types
# ---------------------------------------------------------------------------


class EventType(str, Enum):
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    REASONING = "reasoning"
    PROVIDER_META = "provider_meta"
    ERROR = "error"
    SESSION = "session"


class StopReason(str, Enum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    TIMEOUT = "timeout"
    MAX_STEPS = "max_steps"
    CANCELLED = "cancelled"
    ERROR = "error"


class Event(BaseModel):
    """Base event — every event in the system inherits from this."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    type: EventType
    timestamp: float = Field(default_factory=time.time)
    data: dict[str, Any] = Field(default_factory=dict)

    model_config = {"frozen": False}


class UserMessage(Event):
    type: EventType = EventType.USER_MESSAGE
    content: str = ""
    # Non-empty when the message carries image or mixed content.
    parts: list[ContentBlock] = Field(default_factory=list)

    @property
    def is_multimodal(self) -> bool:
        """Return True when this message contains image (or other non-text) blocks."""
        return bool(self.parts)


class AssistantMessage(Event):
    type: EventType = EventType.ASSISTANT_MESSAGE
    content: str = ""
    stop_reason: StopReason | None = None


class ToolCall(Event):
    type: EventType = EventType.TOOL_CALL
    tool_name: str = ""
    tool_call_id: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(Event):
    type: EventType = EventType.TOOL_RESULT
    tool_call_id: str = ""
    tool_name: str = ""
    output: str = ""
    is_error: bool = False
    duration_ms: float = 0.0


class ReasoningBlock(Event):
    type: EventType = EventType.REASONING
    content: str = ""


class ProviderMeta(Event):
    type: EventType = EventType.PROVIDER_META
    provider: str = ""
    model: str = ""
    usage: dict[str, int] = Field(default_factory=dict)
    request_id: str = ""
    duration_ms: float = 0.0


class ErrorEvent(Event):
    type: EventType = EventType.ERROR
    message: str = ""
    recoverable: bool = True


class SessionEvent(Event):
    type: EventType = EventType.SESSION
    action: str = ""  # "started", "resumed", "paused", "ended"


# Union type for type-safe event handling
AnyEvent = (
    UserMessage
    | AssistantMessage
    | ToolCall
    | ToolResult
    | ReasoningBlock
    | ProviderMeta
    | ErrorEvent
    | SessionEvent
)

EVENT_TYPE_MAP: dict[EventType, type[Event]] = {
    EventType.USER_MESSAGE: UserMessage,
    EventType.ASSISTANT_MESSAGE: AssistantMessage,
    EventType.TOOL_CALL: ToolCall,
    EventType.TOOL_RESULT: ToolResult,
    EventType.REASONING: ReasoningBlock,
    EventType.PROVIDER_META: ProviderMeta,
    EventType.ERROR: ErrorEvent,
    EventType.SESSION: SessionEvent,
}


def deserialize_event(data: dict[str, Any]) -> Event:
    """Reconstruct a typed event from a dict."""
    event_type = EventType(data["type"])
    cls = EVENT_TYPE_MAP[event_type]
    return cls.model_validate(data)
