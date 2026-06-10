"""Shared utilities for compaction and summarization."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── File Operation Tracking ──────────────────────────────────────────────


@dataclass
class FileOperations:
    """Tracks file reads, writes, and edits during a conversation."""

    read: set[str] = field(default_factory=set)
    written: set[str] = field(default_factory=set)
    edited: set[str] = field(default_factory=set)


def create_file_ops() -> FileOperations:
    """Create a fresh FileOperations tracker."""
    return FileOperations()


def extract_file_ops_from_message(message: dict[str, Any], file_ops: FileOperations) -> None:
    """Extract file operations from tool calls in an assistant message."""
    if message.get("role") != "assistant":
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name", "")
        args = block.get("input", {})
        if not isinstance(args, dict):
            continue
        path = args.get("path") or args.get("file_path", "")
        if not path or not isinstance(path, str):
            continue
        if name == "read_file":
            file_ops.read.add(path)
        elif name == "write_file":
            file_ops.written.add(path)
        elif name == "edit_file":
            file_ops.edited.add(path)


def compute_file_lists(
    file_ops: FileOperations,
) -> tuple[list[str], list[str]]:
    """Compute ``(read_only_files, modified_files)`` from file operations.

    Files that were both read and modified appear only in the modified list.
    """
    modified = file_ops.edited | file_ops.written
    read_only = sorted(f for f in file_ops.read if f not in modified)
    modified_sorted = sorted(modified)
    return read_only, modified_sorted


def format_file_operations(read_files: list[str], modified_files: list[str]) -> str:
    """Format file operations as XML tags for the compaction summary."""
    sections: list[str] = []
    if read_files:
        file_list = "\n".join(read_files)
        sections.append(f"<read-files>\n{file_list}\n</read-files>")
    if modified_files:
        file_list = "\n".join(modified_files)
        sections.append(f"<modified-files>\n{file_list}\n</modified-files>")
    if not sections:
        return ""
    return "\n\n" + "\n\n".join(sections)


# ── Message Serialization ────────────────────────────────────────────────

TOOL_RESULT_MAX_CHARS = 2000


def _truncate_for_summary(text: str, max_chars: int) -> str:
    """Truncate text, keeping the beginning and appending a marker."""
    if len(text) <= max_chars:
        return text
    remaining = len(text) - max_chars
    return f"{text[:max_chars]}\n\n[... {remaining} more characters truncated]"


def serialize_conversation(messages: list[dict[str, Any]]) -> str:
    """Serialize provider messages to labelled text for summarization.

    Converts messages to ``[User]: ...`` / ``[Assistant]: ...`` tagged
    blocks so the summarization model sees text, not a conversation it
    should continue.  Tool results are truncated to stay within
    reasonable token budgets.
    """
    parts: list[str] = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "user":
            if isinstance(content, str):
                if content:
                    parts.append(f"[User]: {content}")
            elif isinstance(content, list):
                text_parts: list[str] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_result":
                        rc = str(block.get("content", ""))
                        if rc:
                            tid = block.get("tool_use_id", "")
                            parts.append(
                                f"[Tool result ({tid})]: "
                                f"{_truncate_for_summary(rc, TOOL_RESULT_MAX_CHARS)}"
                            )
                if text_parts:
                    parts.append(f"[User]: {''.join(text_parts)}")

        elif role == "assistant":
            if isinstance(content, str):
                if content:
                    parts.append(f"[Assistant]: {content}")
            elif isinstance(content, list):
                text_parts_a: list[str] = []
                tool_calls: list[str] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text_parts_a.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        name = block.get("name", "")
                        args = block.get("input", {})
                        if isinstance(args, dict):
                            args_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
                        else:
                            args_str = str(args)
                        tool_calls.append(f"{name}({args_str})")
                if text_parts_a:
                    parts.append(f"[Assistant]: {''.join(text_parts_a)}")
                if tool_calls:
                    parts.append(f"[Assistant tool calls]: {'; '.join(tool_calls)}")

    return "\n\n".join(parts)


# ── Summarization System Prompt ──────────────────────────────────────────

SUMMARIZATION_SYSTEM_PROMPT = (
    "You are a context summarization assistant. Your task is to read a "
    "conversation between a user and an AI coding assistant, then produce "
    "a structured summary following the exact format specified.\n\n"
    "Do NOT continue the conversation. Do NOT respond to any questions "
    "in the conversation. ONLY output the structured summary."
)
