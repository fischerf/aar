"""Built-in search tools: grep (regex content search) and find_files (glob path search)."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from agent.tools.registry import ToolRegistry
from agent.tools.schema import SideEffect, ToolSpec

# Directories always skipped during search
_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".eggs",
    "*.egg-info",
}

# Max file size (bytes) to attempt reading during grep
_MAX_FILE_SIZE = 2_000_000  # 2 MB


def _iter_files(root: Path, include_pattern: str = "") -> list[Path]:
    """Walk *root* recursively, skipping hidden/generated dirs and large files.

    When *include_pattern* is given it is treated as a glob matched against
    the path relative to *root* (e.g. ``"**/*.py"`` or ``"src/*.ts"``).
    """
    results: list[Path] = []

    if include_pattern:
        # Use Path.rglob which already handles ** patterns
        for candidate in sorted(root.rglob(include_pattern)):
            if not candidate.is_file():
                continue
            rel_parts = candidate.relative_to(root).parts
            if any(part in _SKIP_DIRS or part.startswith(".") for part in rel_parts[:-1]):
                continue
            try:
                if candidate.stat().st_size > _MAX_FILE_SIZE:
                    continue
            except OSError:
                continue
            results.append(candidate)
        return results

    # Full walk — skip directories early for speed
    dirs_to_walk = [root]
    while dirs_to_walk:
        current = dirs_to_walk.pop()
        children = []
        try:
            children = sorted(current.iterdir(), key=lambda p: p.name.lower())
        except PermissionError:
            continue
        for child in children:
            if child.is_dir():
                name = child.name
                if name in _SKIP_DIRS or name.startswith("."):
                    continue
                # Also skip dirs matching wildcard patterns in _SKIP_DIRS
                if any(fnmatch.fnmatch(name, pat) for pat in _SKIP_DIRS if "*" in pat):
                    continue
                dirs_to_walk.append(child)
            elif child.is_file():
                try:
                    if child.stat().st_size > _MAX_FILE_SIZE:
                        continue
                except OSError:
                    continue
                results.append(child)

    return sorted(results)


def register_search_tools(registry: ToolRegistry) -> None:
    """Register grep and find_files tools into the given registry."""

    async def grep(
        regex: str,
        include_pattern: str = "",
        case_sensitive: bool = False,
        max_results: int = 50,
    ) -> str:
        """Search file contents with a regex pattern.

        Returns matching lines with file paths and line numbers.
        Results are capped at *max_results* to prevent context flooding.
        """
        cwd = Path.cwd()
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(regex, flags)
        except re.error as e:
            raise ValueError(f"Invalid regex: {e}")

        files = _iter_files(cwd, include_pattern)
        matches: list[str] = []
        total_matches = 0

        for filepath in files:
            try:
                text = filepath.read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    total_matches += 1
                    if len(matches) < max_results:
                        rel = filepath.relative_to(cwd)
                        matches.append(f"{rel}:{lineno}: {line.rstrip()}")

        if not matches:
            return "No matches found."

        header = f"Found {total_matches} match{'es' if total_matches != 1 else ''}"
        if total_matches > max_results:
            header += f" (showing first {max_results})"
        header += ":\n"
        return header + "\n".join(matches)

    async def find_files(
        glob_pattern: str,
        max_results: int = 200,
    ) -> str:
        """Find files whose paths match a glob pattern.

        Returns paths relative to the working directory, sorted alphabetically.
        Results are capped at *max_results*.
        """
        cwd = Path.cwd()

        raw_matches = sorted(cwd.rglob(glob_pattern))
        files: list[str] = []
        for m in raw_matches:
            if not m.is_file():
                continue
            rel_parts = m.relative_to(cwd).parts
            # Skip hidden/generated directories (but allow hidden files if explicitly matched)
            if any(part in _SKIP_DIRS or part.startswith(".") for part in rel_parts[:-1]):
                continue
            files.append(str(m.relative_to(cwd)))

        total = len(files)
        if total == 0:
            return f"No files matching '{glob_pattern}' found."

        shown = files[:max_results]
        header = f"Found {total} file{'s' if total != 1 else ''}"
        if total > max_results:
            header += f" (showing first {max_results})"
        header += ":\n"
        return header + "\n".join(shown)

    # --- Register tools ---

    registry.add(
        ToolSpec(
            name="grep",
            description=(
                "Search file contents using a regex pattern. Returns matching lines with "
                "file paths and line numbers. Searches the working directory recursively, "
                "skipping hidden directories, node_modules, __pycache__, and other generated "
                "paths. Use include_pattern to narrow the search to specific file types "
                "(e.g. '**/*.py'). Results are paginated — increase max_results if needed."
            ),
            prompt_snippet="Search file contents with regex",
            prompt_guidelines=[
                "Use grep to search file contents (symbols, patterns); "
                "use find_files for path/filename searches.",
            ],
            input_schema={
                "type": "object",
                "properties": {
                    "regex": {
                        "type": "string",
                        "description": "Regex pattern to search for (Python re syntax).",
                    },
                    "include_pattern": {
                        "type": "string",
                        "description": (
                            "Optional glob to restrict which files are searched, "
                            "e.g. '**/*.py' or 'src/**/*.ts'. Default: all files."
                        ),
                        "default": "",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": (
                            "Whether the regex match is case-sensitive. Default: false."
                        ),
                        "default": False,
                    },
                    "max_results": {
                        "type": "integer",
                        "description": ("Maximum number of matching lines to return. Default: 50."),
                        "default": 50,
                    },
                },
                "required": ["regex"],
            },
            side_effects=[SideEffect.READ],
            handler=grep,
        )
    )

    registry.add(
        ToolSpec(
            name="find_files",
            description=(
                "Find files by glob pattern. Returns file paths relative to the working "
                "directory. Searches recursively, skipping hidden and generated directories. "
                "Use patterns like '*.py', '**/*.test.js', or 'src/**/*.ts'."
            ),
            prompt_snippet="Find files by glob pattern",
            input_schema={
                "type": "object",
                "properties": {
                    "glob_pattern": {
                        "type": "string",
                        "description": (
                            "Glob pattern to match file paths, e.g. '**/*.py' or '*.md'."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": ("Maximum number of file paths to return. Default: 200."),
                        "default": 200,
                    },
                },
                "required": ["glob_pattern"],
            },
            side_effects=[SideEffect.READ],
            handler=find_files,
        )
    )
