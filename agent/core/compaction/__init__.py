"""Context compaction for long sessions.

Provides LLM-based summarization of older conversation messages so that
context stays within the model's window without simply dropping history.
"""

from agent.core.compaction.compaction import (
    CompactionResult,
    compact_session,
    estimate_context_tokens,
    estimate_event_tokens,
    estimate_message_tokens,
    find_event_cut_point,
    generate_summary,
    should_compact,
)
from agent.core.compaction.utils import (
    SUMMARIZATION_SYSTEM_PROMPT,
    FileOperations,
    compute_file_lists,
    create_file_ops,
    extract_file_ops_from_message,
    format_file_operations,
    serialize_conversation,
)

__all__ = [
    "CompactionResult",
    "FileOperations",
    "SUMMARIZATION_SYSTEM_PROMPT",
    "compact_session",
    "compute_file_lists",
    "create_file_ops",
    "estimate_context_tokens",
    "estimate_event_tokens",
    "estimate_message_tokens",
    "extract_file_ops_from_message",
    "find_event_cut_point",
    "format_file_operations",
    "generate_summary",
    "serialize_conversation",
    "should_compact",
]
