"""Session management — owns conversation history and metadata."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field

from agent.core.events import (
    AnyEvent,
    AssistantMessage,
    Event,
    SessionEvent,
    ToolCall,
    ToolResult,
    UserMessage,
)
from agent.core.state import AgentState


class Session(BaseModel):
    """Serializable session that tracks the full conversation lifecycle."""

    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: AgentState = AgentState.IDLE
    events: list[Event] = Field(default_factory=list)
    step_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)

    def append(self, event: Event | list[Event]) -> None:
        """Append one or more events to the session."""
        if isinstance(event, list):
            self.events.extend(event)
        else:
            self.events.append(event)

    def add_user_message(self, content: str) -> UserMessage:
        msg = UserMessage(content=content)
        self.append(msg)
        return msg

    def add_assistant_message(self, content: str, **kwargs: Any) -> AssistantMessage:
        msg = AssistantMessage(content=content, **kwargs)
        self.append(msg)
        return msg

    def add_tool_call(self, **kwargs: Any) -> ToolCall:
        tc = ToolCall(**kwargs)
        self.append(tc)
        return tc

    def add_tool_result(self, **kwargs: Any) -> ToolResult:
        tr = ToolResult(**kwargs)
        self.append(tr)
        return tr

    def increment_step(self) -> int:
        self.step_count += 1
        return self.step_count

    def to_messages(self) -> list[dict[str, Any]]:
        """Convert session events to a provider-friendly message list.

        Returns a list of message dicts with role/content structure,
        grouping tool calls and results into the appropriate messages.
        """
        messages: list[dict[str, Any]] = []
        pending_tool_calls: list[ToolCall] = []
        pending_tool_results: list[ToolResult] = []

        for event in self.events:
            if isinstance(event, UserMessage):
                # Flush any pending tool results first
                if pending_tool_results:
                    messages.append(_tool_results_message(pending_tool_results))
                    pending_tool_results = []
                messages.append({"role": "user", "content": event.content})

            elif isinstance(event, AssistantMessage):
                if pending_tool_calls:
                    # Assistant message with tool calls
                    content_blocks: list[dict] = []
                    if event.content:
                        content_blocks.append({"type": "text", "text": event.content})
                    for tc in pending_tool_calls:
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tc.tool_call_id,
                            "name": tc.tool_name,
                            "input": tc.arguments,
                        })
                    messages.append({"role": "assistant", "content": content_blocks})
                    pending_tool_calls = []
                else:
                    messages.append({"role": "assistant", "content": event.content})

            elif isinstance(event, ToolCall):
                pending_tool_calls.append(event)

            elif isinstance(event, ToolResult):
                pending_tool_results.append(event)

        # Flush remaining
        if pending_tool_calls:
            content_blocks = []
            for tc in pending_tool_calls:
                content_blocks.append({
                    "type": "tool_use",
                    "id": tc.tool_call_id,
                    "name": tc.tool_name,
                    "input": tc.arguments,
                })
            messages.append({"role": "assistant", "content": content_blocks})

        if pending_tool_results:
            messages.append(_tool_results_message(pending_tool_results))

        return messages


def _tool_results_message(results: list[ToolResult]) -> dict[str, Any]:
    """Build a user message containing tool result blocks."""
    content = []
    for tr in results:
        content.append({
            "type": "tool_result",
            "tool_use_id": tr.tool_call_id,
            "content": tr.output,
            "is_error": tr.is_error,
        })
    return {"role": "user", "content": content}
