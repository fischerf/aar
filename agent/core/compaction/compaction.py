"""Context compaction for long sessions.

When conversations grow large, this module detects the threshold, calls
the LLM to generate a structured summary of older messages, and replaces
them with the summary so the context stays within the model's window.

Pure functions for compaction logic.  The session manager handles I/O;
after compaction the session events are updated in-place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agent.core.compaction.utils import (
    SUMMARIZATION_SYSTEM_PROMPT,
    compute_file_lists,
    create_file_ops,
    extract_file_ops_from_message,
    format_file_operations,
    serialize_conversation,
)
from agent.core.events import (
    AssistantMessage,
    Event,
    ProviderMeta,
    ToolCall,
    ToolResult,
    UserMessage,
)

if TYPE_CHECKING:
    from agent.core.config import CompactionConfig
    from agent.core.session import Session
    from agent.providers.base import Provider

logger = logging.getLogger(__name__)


# ── Token Estimation ─────────────────────────────────────────────────────


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """Estimate token count for a single provider message dict (chars / 4).

    This is a fast heuristic, not an exact count.  It intentionally
    over-estimates so compaction triggers slightly early rather than late.
    """
    chars = 0
    content = message.get("content", "")
    if isinstance(content, str):
        chars = len(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text":
                chars += len(block.get("text", ""))
            elif btype == "tool_use":
                chars += len(block.get("name", ""))
                chars += len(str(block.get("input", {})))
            elif btype == "tool_result":
                chars += len(str(block.get("content", "")))
            elif btype == "image":
                chars += 4800  # ~1 200 tokens per image
    return max(1, chars // 4)


def estimate_event_tokens(event: Event) -> int:
    """Estimate tokens for a single session event (chars / 4)."""
    if isinstance(event, UserMessage):
        if event.is_multimodal:
            chars = sum(len(getattr(p, "text", "")) for p in event.parts)
        else:
            chars = len(event.content)
    elif isinstance(event, AssistantMessage):
        chars = len(event.content)
    elif isinstance(event, ToolCall):
        chars = len(event.tool_name) + len(str(event.arguments))
    elif isinstance(event, ToolResult):
        chars = len(event.output)
    else:
        return 0
    return max(1, chars // 4)


def estimate_context_tokens(
    messages: list[dict[str, Any]],
    last_usage: dict[str, int] | None = None,
) -> int:
    """Estimate total context tokens.

    When *last_usage* is provided (from the most recent
    :class:`~agent.core.events.ProviderMeta`), real usage data is
    preferred.  Otherwise falls back to the chars/4 heuristic.
    """
    if last_usage:
        total = (
            last_usage.get("input_tokens", 0)
            + last_usage.get("output_tokens", 0)
            + last_usage.get("cache_read_tokens", 0)
            + last_usage.get("cache_write_tokens", 0)
        )
        if total > 0:
            return total
    return sum(estimate_message_tokens(m) for m in messages)


# ── Compaction Trigger ───────────────────────────────────────────────────


def should_compact(
    context_tokens: int,
    context_window: int,
    reserve_tokens: int,
) -> bool:
    """Return True when context has grown past the compaction threshold."""
    if context_window <= 0:
        return False
    return context_tokens > context_window - reserve_tokens


# ── Cut-point Detection ─────────────────────────────────────────────────


def find_event_cut_point(
    events: list[Event],
    keep_recent_tokens: int,
) -> int:
    """Find the event index from which to start keeping events.

    Walks backwards through events, accumulating estimated tokens.
    Stops when ``keep_recent_tokens`` is exceeded and returns the
    nearest *UserMessage* boundary (the start of a turn) — this
    guarantees we never cut in the middle of a tool-call/result cycle.

    Returns 0 when everything fits (nothing to compact).
    """
    if len(events) <= 2:
        return 0

    accumulated = 0

    for i in range(len(events) - 1, -1, -1):
        accumulated += estimate_event_tokens(events[i])

        if accumulated >= keep_recent_tokens:
            # Walk forward from *i* to find the nearest UserMessage boundary
            for j in range(i, len(events)):
                if isinstance(events[j], UserMessage):
                    return j
            # No UserMessage found after i — try cutting at i itself
            return i

    return 0  # everything fits


# ── Summarization Prompts ────────────────────────────────────────────────

_INITIAL_PROMPT = """\
The messages above are a conversation to summarize. Create a structured \
context checkpoint summary that another LLM will use to continue the work.

Use this EXACT format:

## Goal
[What is the user trying to accomplish?]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned by user]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Any data, examples, or references needed to continue]
- [Or "(none)" if not applicable]

Keep each section concise. Preserve exact file paths, function names, \
and error messages."""

_UPDATE_PROMPT = """\
The messages above are NEW conversation messages to incorporate into the \
existing summary provided in <previous-summary> tags.

Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context from the new messages
- UPDATE the Progress section: move items from "In Progress" to "Done" \
when completed
- UPDATE "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, and error messages
- If something is no longer relevant, you may remove it

Use the same structured format as the previous summary.

Keep each section concise. Preserve exact file paths, function names, \
and error messages."""


# ── Summary Generation ───────────────────────────────────────────────────


@dataclass
class CompactionResult:
    """Outcome of a successful compaction."""

    summary: str
    tokens_before: int
    events_removed: int
    read_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)


async def generate_summary(
    messages_to_summarize: list[dict[str, Any]],
    provider: "Provider",
    reserve_tokens: int = 16_384,
    previous_summary: str | None = None,
) -> str:
    """Call the LLM to produce a structured summary of the given messages."""
    conversation_text = serialize_conversation(messages_to_summarize)

    prompt_parts = [f"<conversation>\n{conversation_text}\n</conversation>\n"]
    if previous_summary:
        prompt_parts.append(f"<previous-summary>\n{previous_summary}\n</previous-summary>\n")
        prompt_parts.append(_UPDATE_PROMPT)
    else:
        prompt_parts.append(_INITIAL_PROMPT)

    response = await provider.complete(
        messages=[{"role": "user", "content": "\n".join(prompt_parts)}],
        tools=None,
        system=SUMMARIZATION_SYSTEM_PROMPT,
    )
    return response.content


# ── Session-level Compaction ─────────────────────────────────────────────


def _get_last_usage(session: "Session") -> dict[str, int] | None:
    """Extract the most recent ProviderMeta usage from session events."""
    for event in reversed(session.events):
        if isinstance(event, ProviderMeta) and event.usage:
            return event.usage
    return None


async def compact_session(
    session: "Session",
    provider: "Provider",
    context_window: int,
    config: "CompactionConfig",
) -> CompactionResult | None:
    """Check if compaction is needed and apply it to the session.

    Inspects the current session events, estimates context token usage,
    and — when the context exceeds ``context_window - reserve_tokens`` —
    generates an LLM summary of older messages and replaces them in
    ``session.events``.

    Returns a :class:`CompactionResult` when compaction was performed,
    or ``None`` when the context is still within bounds.
    """
    from agent.core.session import events_to_messages

    messages = session.to_messages()

    # Estimate current context size
    last_usage = _get_last_usage(session)
    ctx_tokens = estimate_context_tokens(messages, last_usage)

    if not should_compact(ctx_tokens, context_window, config.reserve_tokens):
        return None

    # Find where to cut in the event list
    cut_index = find_event_cut_point(session.events, config.keep_recent_tokens)
    if cut_index <= 0:
        return None

    # Convert events-before-cut to messages for the summarization LLM
    events_to_summarize = session.events[:cut_index]
    messages_to_summarize = events_to_messages(events_to_summarize)
    if not messages_to_summarize:
        return None

    logger.info(
        "Compacting: %d events (%d messages) → summary + %d kept events",
        cut_index,
        len(messages_to_summarize),
        len(session.events) - cut_index,
    )

    # Retrieve previous summary for iterative update
    previous_summary: str | None = session.metadata.get("compaction_summary")

    # Generate structured summary via LLM
    summary = await generate_summary(
        messages_to_summarize,
        provider,
        reserve_tokens=config.reserve_tokens,
        previous_summary=previous_summary,
    )

    # File operation tracking
    file_ops = create_file_ops()
    for msg in messages_to_summarize:
        extract_file_ops_from_message(msg, file_ops)
    read_files, modified_files = compute_file_lists(file_ops)
    summary += format_file_operations(read_files, modified_files)

    # Apply compaction to the session
    events_removed = cut_index
    summary_content = (
        f"[Context from earlier conversation — "
        f"{ctx_tokens} estimated tokens compacted]\n\n"
        f"{summary}"
    )
    session.apply_compaction(cut_index, summary_content)
    session.metadata["compaction_summary"] = summary

    logger.info(
        "Compaction complete: %d events removed, summary stored",
        events_removed,
    )

    return CompactionResult(
        summary=summary,
        tokens_before=ctx_tokens,
        events_removed=events_removed,
        read_files=read_files,
        modified_files=modified_files,
    )
