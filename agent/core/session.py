"""Session management — owns conversation history and metadata."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field

from agent.core.events import (
    AssistantMessage,
    ContentBlock,
    Event,
    TextBlock,
    ToolCall,
    ToolResult,
    UserMessage,
)
from agent.core.state import AgentState


class Session(BaseModel):
    """Serializable session that tracks the full conversation lifecycle."""

    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    trace_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: AgentState = AgentState.IDLE
    events: list[Event] = Field(default_factory=list)
    step_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost: float = 0.0

    @property
    def total_tokens(self) -> int:
        """Total tokens consumed across all steps."""
        return self.total_input_tokens + self.total_output_tokens

    def append(self, event: Event | list[Event]) -> None:
        """Append one or more events to the session."""
        if isinstance(event, list):
            self.events.extend(event)
        else:
            self.events.append(event)

    def add_user_message(self, content: str | list[ContentBlock]) -> UserMessage:
        """Add a user message to the session.

        *content* may be a plain string (text-only) or a list of
        :class:`~agent.core.events.ContentBlock` objects (text + images).
        When a list is provided, a plain-text summary derived from the
        :class:`~agent.core.events.TextBlock` parts is stored in
        ``UserMessage.content`` for logging and display, while the full
        block list is stored in ``UserMessage.parts`` for the provider
        adapters.
        """
        if isinstance(content, str):
            msg = UserMessage(content=content)
        else:
            text_summary = " ".join(b.text for b in content if isinstance(b, TextBlock))
            msg = UserMessage(content=text_summary, parts=content)
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
        Multimodal user messages emit a content *list* instead of a plain
        string so that every provider adapter receives properly structured
        image blocks.
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
                if event.is_multimodal:
                    messages.append(
                        {
                            "role": "user",
                            "content": [p.model_dump(exclude_none=True) for p in event.parts],
                        }
                    )
                else:
                    messages.append({"role": "user", "content": event.content})

            elif isinstance(event, AssistantMessage):
                # Flush pending tool results before the next assistant message
                if pending_tool_results:
                    messages.append(_tool_results_message(pending_tool_results))
                    pending_tool_results = []

                if pending_tool_calls:
                    # Assistant message with tool calls
                    content_blocks: list[dict] = []
                    if event.content:
                        content_blocks.append({"type": "text", "text": event.content})
                    for tc in pending_tool_calls:
                        content_blocks.append(
                            {
                                "type": "tool_use",
                                "id": tc.tool_call_id,
                                "name": tc.tool_name,
                                "input": tc.arguments,
                            }
                        )
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
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.tool_call_id,
                        "name": tc.tool_name,
                        "input": tc.arguments,
                    }
                )
            messages.append({"role": "assistant", "content": content_blocks})

        if pending_tool_results:
            messages.append(_tool_results_message(pending_tool_results))

        return messages


def estimate_token_count(messages: list[dict[str, Any]]) -> int:
    """Rough token estimate: ~4 chars per token for English text.

    This is a fast heuristic, not an exact count.  It's used to decide
    whether to trim context, not for billing.
    """
    total_chars = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total_chars += len(str(block.get("text", "")))
                    total_chars += len(str(block.get("content", "")))
                    total_chars += len(str(block.get("input", "")))
    return total_chars // 4


def trim_to_token_budget(
    messages: list[dict[str, Any]],
    max_tokens: int,
) -> list[dict[str, Any]]:
    """Drop oldest messages (keeping the first user message) until under budget.

    Preserves the most recent messages.  If even the last message exceeds
    the budget, returns it alone — we never return an empty list.
    """
    if max_tokens <= 0 or estimate_token_count(messages) <= max_tokens:
        return messages

    # Always try to keep the last N messages that fit
    result: list[dict[str, Any]] = []
    budget = max_tokens
    for msg in reversed(messages):
        msg_tokens = estimate_token_count([msg])
        if budget - msg_tokens < 0 and result:
            break
        result.insert(0, msg)
        budget -= msg_tokens

    return result


def _tool_results_message(results: list[ToolResult]) -> dict[str, Any]:
    """Build a user message containing tool result blocks."""
    content = []
    for tr in results:
        content.append(
            {
                "type": "tool_result",
                "tool_use_id": tr.tool_call_id,
                "content": tr.output,
                "is_error": tr.is_error,
            }
        )
    return {"role": "user", "content": content}
