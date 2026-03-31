"""Built-in filesystem tools: read, write, edit, list directory."""

from __future__ import annotations

import os
from pathlib import Path

from agent.tools.registry import ToolRegistry
from agent.tools.schema import SideEffect, ToolSpec


def register_filesystem_tools(registry: ToolRegistry) -> None:
    """Register all filesystem tools into the given registry."""

    async def read_file(path: str) -> str:
        """Read a file and return its contents."""
        p = Path(path).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        content = p.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines(keepends=True)
        numbered = "".join(f"{i + 1:>6}\t{line}" for i, line in enumerate(lines))
        return numbered

    async def write_file(path: str, content: str) -> str:
        """Write content to a file, creating directories as needed."""
        p = Path(path).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"

    async def edit_file(path: str, old_string: str, new_string: str) -> str:
        """Replace an exact string in a file."""
        p = Path(path).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        text = p.read_text(encoding="utf-8")
        count = text.count(old_string)
        if count == 0:
            raise ValueError(f"old_string not found in {path}")
        if count > 1:
            raise ValueError(f"old_string found {count} times in {path} — must be unique")
        new_text = text.replace(old_string, new_string, 1)
        p.write_text(new_text, encoding="utf-8")
        return f"Edited {path}: replaced 1 occurrence"

    async def list_directory(path: str = ".") -> str:
        """List files and directories at the given path."""
        p = Path(path).resolve()
        if not p.is_dir():
            raise NotADirectoryError(f"Not a directory: {path}")
        entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        lines = []
        for entry in entries:
            prefix = "d " if entry.is_dir() else "f "
            size = ""
            if entry.is_file():
                size = f"  ({entry.stat().st_size} bytes)"
            lines.append(f"{prefix}{entry.name}{size}")
        return "\n".join(lines) if lines else "(empty directory)"

    registry.add(ToolSpec(
        name="read_file",
        description="Read a file and return its contents with line numbers.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute or relative file path"}},
            "required": ["path"],
        },
        side_effects=[SideEffect.READ],
        handler=read_file,
    ))

    registry.add(ToolSpec(
        name="write_file",
        description="Write content to a file. Creates parent directories if needed.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to write to"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        },
        side_effects=[SideEffect.WRITE],
        handler=write_file,
    ))

    registry.add(ToolSpec(
        name="edit_file",
        description="Replace an exact string in a file. The old_string must appear exactly once.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to edit"},
                "old_string": {"type": "string", "description": "Exact string to find"},
                "new_string": {"type": "string", "description": "Replacement string"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        side_effects=[SideEffect.WRITE],
        handler=edit_file,
    ))

    registry.add(ToolSpec(
        name="list_directory",
        description="List files and directories at a given path.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Directory path (default: current directory)"}},
            "required": [],
        },
        side_effects=[SideEffect.READ],
        handler=list_directory,
    ))
