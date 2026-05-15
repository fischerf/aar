"""Built-in filesystem tools: read, write, edit, list directory."""

from __future__ import annotations

import os
from pathlib import Path

from agent.tools.registry import ToolRegistry
from agent.tools.schema import SideEffect, ToolSpec

# Extensions that must keep LF line-endings even on Windows (WSL compat)
_LF_EXTENSIONS = frozenset(
    {
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".py",
        ".rb",
        ".pl",
        ".lua",
        ".yml",
        ".yaml",
        ".toml",
        ".json",
        ".jsonl",
    }
)
_LF_FILENAMES = frozenset({"Makefile", "Dockerfile", ".gitattributes"})


def _should_force_lf(p: Path, content: str) -> bool:
    """Return True when the file should be written with LF, not CRLF.

    On Windows + WSL, CRLF in shell scripts causes 'set: -\\r: invalid option'.
    We force LF for known script/config extensions, shebang-bearing files, and
    common filenames that must stay Unix-compatible.
    """
    if p.suffix.lower() in _LF_EXTENSIONS:
        return True
    if p.name in _LF_FILENAMES:
        return True
    if content.startswith("#!"):  # shebang
        return True
    return False


def register_filesystem_tools(registry: ToolRegistry) -> None:
    """Register all filesystem tools into the given registry."""

    async def read_file(path: str, start_line: int = 0, end_line: int = 0) -> str:
        """Read a file and return its contents with line numbers.

        When *start_line* / *end_line* are provided, only that slice is returned
        (1-based, inclusive).  When omitted (or 0), the entire file is returned.

        For files exceeding 500 lines with no line range specified, an outline
        summary is returned instead of the full content, showing line counts and
        a hint to use start_line/end_line.
        """
        p = Path(path).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"File not found: {p}")
        content = p.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines(keepends=True)
        total = len(lines)

        # Determine slice bounds (1-based inclusive → 0-based)
        s = max(start_line - 1, 0) if start_line > 0 else 0
        e = min(end_line, total) if end_line > 0 else total

        if s >= total:
            return f"start_line {start_line} is beyond end of file ({total} lines)."

        # If no range specified and file is large, return a summary
        if start_line <= 0 and end_line <= 0 and total > 500:
            return (
                f"File {p} has {total} lines — too large to return in full.\n"
                f"Use start_line / end_line to read a specific section.\n"
                f"First 50 lines preview:\n\n"
                + "".join(f"{i + 1:>6}\t{lines[i]}" for i in range(min(50, total)))
            )

        selected = lines[s:e]
        numbered = "".join(f"{s + i + 1:>6}\t{line}" for i, line in enumerate(selected))
        if start_line > 0 or end_line > 0:
            shown_range = f"[lines {s + 1}–{s + len(selected)} of {total}]"
            return f"{shown_range}\n{numbered}"
        return numbered

    async def write_file(path: str, content: str) -> str:
        """Write content to a file, creating directories as needed."""
        p = Path(path).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt" and _should_force_lf(p, content):
            # Write raw bytes to avoid Python's text-mode CRLF conversion.
            # The LLM sends \n — we must not let Windows corrupt that to \r\n.
            p.write_bytes(content.encode("utf-8"))
        else:
            p.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {p}"

    async def edit_file(path: str, old_string: str, new_string: str) -> str:
        """Replace an exact string in a file."""
        p = Path(path).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"File not found: {p}")
        raw = p.read_bytes()
        crlf = b"\r\n" in raw
        text = raw.decode("utf-8")
        # Normalize to LF for matching so the model's \n-based strings always work
        norm_text = text.replace("\r\n", "\n")
        norm_old = old_string.replace("\r\n", "\n")
        norm_new = new_string.replace("\r\n", "\n")
        count = norm_text.count(norm_old)
        if count == 0:
            raise ValueError(f"old_string not found in {p}")
        if count > 1:
            raise ValueError(f"old_string found {count} times in {p} — must be unique")
        norm_result = norm_text.replace(norm_old, norm_new, 1)
        # Restore original line endings
        result = norm_result.replace("\n", "\r\n") if crlf else norm_result
        p.write_bytes(result.encode("utf-8"))
        return f"Edited {p}: replaced 1 occurrence"

    async def list_directory(path: str = ".") -> str:
        """List files and directories at the given path."""
        p = Path(path).resolve()
        if not p.is_dir():
            raise NotADirectoryError(f"Not a directory: {p}")
        entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        lines = [f"Contents of {p}:", ""]
        for entry in entries:
            prefix = "d " if entry.is_dir() else "f "
            size = ""
            if entry.is_file():
                size = f"  ({entry.stat().st_size} bytes)"
            lines.append(f"{prefix}{entry.name}{size}")
        return "\n".join(lines) if len(lines) > 2 else f"Contents of {p}:\n\n(empty directory)"

    registry.add(
        ToolSpec(
            name="read_file",
            description=(
                "Read a file and return its contents with line numbers. "
                "Large files (>500 lines) return a preview — use start_line/end_line to read sections."
            ),
            prompt_snippet=(
                "Read file contents (supports line ranges; large files return a preview)"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path, e.g. README.md or subdir\\file.py",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": (
                            "First line to return (1-based, inclusive). "
                            "Omit or pass 0 to start from the beginning."
                        ),
                        "default": 0,
                    },
                    "end_line": {
                        "type": "integer",
                        "description": (
                            "Last line to return (1-based, inclusive). "
                            "Omit or pass 0 to read to the end."
                        ),
                        "default": 0,
                    },
                },
                "required": ["path"],
            },
            side_effects=[SideEffect.READ],
            handler=read_file,
        )
    )

    registry.add(
        ToolSpec(
            name="write_file",
            description="Write content to a file. Creates parent directories if needed. Use paths relative to the working directory, e.g. src\\main.py.",
            prompt_snippet="Create or overwrite a file",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path to write to, e.g. hello.py or subdir\\hello.py",
                    },
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
            side_effects=[SideEffect.WRITE],
            handler=write_file,
        )
    )

    registry.add(
        ToolSpec(
            name="edit_file",
            description="Replace an exact string in a file. The old_string must appear exactly once. Use paths relative to the working directory.",
            prompt_snippet="Replace an exact unique string in a file",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path to edit, e.g. src\\main.py",
                    },
                    "old_string": {"type": "string", "description": "Exact string to find"},
                    "new_string": {"type": "string", "description": "Replacement string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
            side_effects=[SideEffect.WRITE],
            handler=edit_file,
        )
    )

    registry.add(
        ToolSpec(
            name="list_directory",
            description="List files and directories at a given path. Shows the resolved absolute path. Defaults to the current working directory.",
            prompt_snippet="List files and directories at a path",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path (default: current directory), e.g. . or subdir",
                    }
                },
                "required": [],
            },
            side_effects=[SideEffect.READ],
            handler=list_directory,
        )
    )
