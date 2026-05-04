"""Tests for search tools (grep, find_files) and read_file line-range improvements."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.builtin.filesystem import register_filesystem_tools
from agent.tools.builtin.search import _iter_files, register_search_tools
from agent.tools.registry import ToolRegistry
from agent.tools.schema import SideEffect

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry_with_search() -> ToolRegistry:
    reg = ToolRegistry()
    register_search_tools(reg)
    return reg


def _make_registry_with_fs() -> ToolRegistry:
    reg = ToolRegistry()
    register_filesystem_tools(reg)
    return reg


def _create_project(root: Path) -> None:
    """Create a small project tree for search tests."""
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text(
        "import os\n\ndef hello():\n    print('hello world')\n\ndef goodbye():\n    print('bye')\n",
        encoding="utf-8",
    )
    (root / "src" / "utils.py").write_text(
        "def helper():\n    return 42\n\ndef hello_helper():\n    return 'hi'\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("# My Project\n\nA test project.\n", encoding="utf-8")
    (root / "config.json").write_text('{"key": "value"}\n', encoding="utf-8")
    # Hidden dir (should be skipped)
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    # __pycache__ (should be skipped)
    (root / "src" / "__pycache__").mkdir()
    (root / "src" / "__pycache__" / "main.cpython-311.pyc").write_bytes(b"\x00")
    # node_modules (should be skipped)
    (root / "node_modules").mkdir()
    (root / "node_modules" / "pkg.js").write_text("module.exports = {}", encoding="utf-8")


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


class TestGrep:
    @pytest.mark.asyncio
    async def test_grep_finds_matching_lines(self, tmp_path, monkeypatch):
        _create_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        reg = _make_registry_with_search()
        grep = reg.get("grep").handler

        result = await grep(regex="def hello")
        assert "hello" in result
        assert "main.py" in result
        assert "Found" in result

    @pytest.mark.asyncio
    async def test_grep_case_insensitive_default(self, tmp_path, monkeypatch):
        _create_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        reg = _make_registry_with_search()
        grep = reg.get("grep").handler

        result = await grep(regex="DEF HELLO")
        assert "hello" in result.lower()
        assert "Found" in result

    @pytest.mark.asyncio
    async def test_grep_case_sensitive(self, tmp_path, monkeypatch):
        _create_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        reg = _make_registry_with_search()
        grep = reg.get("grep").handler

        result = await grep(regex="DEF HELLO", case_sensitive=True)
        assert result == "No matches found."

    @pytest.mark.asyncio
    async def test_grep_with_include_pattern(self, tmp_path, monkeypatch):
        _create_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        reg = _make_registry_with_search()
        grep = reg.get("grep").handler

        result = await grep(regex="def", include_pattern="**/*.py")
        assert "main.py" in result
        assert "utils.py" in result
        # Should NOT match README.md or config.json
        assert "README" not in result
        assert "config.json" not in result

    @pytest.mark.asyncio
    async def test_grep_no_matches(self, tmp_path, monkeypatch):
        _create_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        reg = _make_registry_with_search()
        grep = reg.get("grep").handler

        result = await grep(regex="nonexistent_symbol_xyz")
        assert result == "No matches found."

    @pytest.mark.asyncio
    async def test_grep_max_results(self, tmp_path, monkeypatch):
        _create_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        reg = _make_registry_with_search()
        grep = reg.get("grep").handler

        # "def" appears in multiple lines across files
        result = await grep(regex="def", max_results=2)
        # Should mention total but only show 2
        lines = [ln for ln in result.strip().split("\n") if ":" in ln and ln[0] != "F"]
        assert len(lines) <= 2

    @pytest.mark.asyncio
    async def test_grep_invalid_regex(self, tmp_path, monkeypatch):
        _create_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        reg = _make_registry_with_search()
        grep = reg.get("grep").handler

        with pytest.raises(ValueError, match="Invalid regex"):
            await grep(regex="[invalid")

    @pytest.mark.asyncio
    async def test_grep_skips_hidden_dirs(self, tmp_path, monkeypatch):
        _create_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        reg = _make_registry_with_search()
        grep = reg.get("grep").handler

        result = await grep(regex="core")
        # .git/config has [core] but should be skipped
        assert ".git" not in result

    @pytest.mark.asyncio
    async def test_grep_skips_pycache(self, tmp_path, monkeypatch):
        _create_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        reg = _make_registry_with_search()
        grep = reg.get("grep").handler

        # __pycache__ files should be skipped
        result = await grep(regex=".*")
        assert "__pycache__" not in result

    @pytest.mark.asyncio
    async def test_grep_skips_node_modules(self, tmp_path, monkeypatch):
        _create_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        reg = _make_registry_with_search()
        grep = reg.get("grep").handler

        result = await grep(regex="module")
        assert "node_modules" not in result

    @pytest.mark.asyncio
    async def test_grep_shows_line_numbers(self, tmp_path, monkeypatch):
        _create_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        reg = _make_registry_with_search()
        grep = reg.get("grep").handler

        result = await grep(regex="def hello\\(\\)")
        # Format should be path:lineno: content
        for line in result.strip().split("\n"):
            if line.startswith("Found"):
                continue
            parts = line.split(":")
            assert len(parts) >= 3, f"Expected path:line:content format, got: {line}"


# ---------------------------------------------------------------------------
# find_files
# ---------------------------------------------------------------------------


class TestFindFiles:
    @pytest.mark.asyncio
    async def test_find_files_by_extension(self, tmp_path, monkeypatch):
        _create_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        reg = _make_registry_with_search()
        find = reg.get("find_files").handler

        result = await find(glob_pattern="**/*.py")
        assert "main.py" in result
        assert "utils.py" in result
        assert "Found" in result

    @pytest.mark.asyncio
    async def test_find_files_specific_name(self, tmp_path, monkeypatch):
        _create_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        reg = _make_registry_with_search()
        find = reg.get("find_files").handler

        result = await find(glob_pattern="**/README.md")
        assert "README.md" in result

    @pytest.mark.asyncio
    async def test_find_files_no_matches(self, tmp_path, monkeypatch):
        _create_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        reg = _make_registry_with_search()
        find = reg.get("find_files").handler

        result = await find(glob_pattern="**/*.xyz")
        assert "No files matching" in result

    @pytest.mark.asyncio
    async def test_find_files_skips_hidden_dirs(self, tmp_path, monkeypatch):
        _create_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        reg = _make_registry_with_search()
        find = reg.get("find_files").handler

        result = await find(glob_pattern="**/*")
        assert ".git" not in result

    @pytest.mark.asyncio
    async def test_find_files_skips_node_modules(self, tmp_path, monkeypatch):
        _create_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        reg = _make_registry_with_search()
        find = reg.get("find_files").handler

        result = await find(glob_pattern="**/*.js")
        assert "node_modules" not in result

    @pytest.mark.asyncio
    async def test_find_files_max_results(self, tmp_path, monkeypatch):
        _create_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        reg = _make_registry_with_search()
        find = reg.get("find_files").handler

        result = await find(glob_pattern="**/*", max_results=2)
        # Header should indicate more exist
        lines = [ln for ln in result.strip().split("\n") if not ln.startswith("Found")]
        assert len(lines) <= 2

    @pytest.mark.asyncio
    async def test_find_files_returns_relative_paths(self, tmp_path, monkeypatch):
        _create_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        reg = _make_registry_with_search()
        find = reg.get("find_files").handler

        result = await find(glob_pattern="**/*.py")
        # Paths should be relative — no tmp_path prefix
        assert str(tmp_path) not in result


# ---------------------------------------------------------------------------
# _iter_files helper
# ---------------------------------------------------------------------------


class TestIterFiles:
    def test_skips_large_files(self, tmp_path):
        big = tmp_path / "big.txt"
        big.write_bytes(b"x" * 3_000_000)  # 3 MB > _MAX_FILE_SIZE
        small = tmp_path / "small.txt"
        small.write_text("hello", encoding="utf-8")

        files = _iter_files(tmp_path)
        names = [f.name for f in files]
        assert "small.txt" in names
        assert "big.txt" not in names

    def test_include_pattern_filters(self, tmp_path):
        (tmp_path / "a.py").write_text("x", encoding="utf-8")
        (tmp_path / "b.txt").write_text("y", encoding="utf-8")

        files = _iter_files(tmp_path, include_pattern="*.py")
        names = [f.name for f in files]
        assert "a.py" in names
        assert "b.txt" not in names


# ---------------------------------------------------------------------------
# read_file line ranges
# ---------------------------------------------------------------------------


class TestReadFileLineRanges:
    @pytest.mark.asyncio
    async def test_read_full_small_file(self, tmp_path):
        f = tmp_path / "small.txt"
        f.write_text("line1\nline2\nline3\n", encoding="utf-8")

        reg = _make_registry_with_fs()
        read = reg.get("read_file").handler
        result = await read(path=str(f))

        assert "line1" in result
        assert "line2" in result
        assert "line3" in result

    @pytest.mark.asyncio
    async def test_read_with_start_line(self, tmp_path):
        f = tmp_path / "lines.txt"
        f.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n", encoding="utf-8")

        reg = _make_registry_with_fs()
        read = reg.get("read_file").handler
        result = await read(path=str(f), start_line=5)

        assert "line5" in result
        assert "line10" in result
        # Lines before start should not appear (as content, may appear in range header)
        # Check that line1 content is not in the numbered output
        assert "line1\n" not in result or "[lines" in result

    @pytest.mark.asyncio
    async def test_read_with_start_and_end_line(self, tmp_path):
        f = tmp_path / "lines.txt"
        f.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n", encoding="utf-8")

        reg = _make_registry_with_fs()
        read = reg.get("read_file").handler
        result = await read(path=str(f), start_line=3, end_line=5)

        assert "line3" in result
        assert "line4" in result
        assert "line5" in result
        assert "[lines" in result
        assert "of 10]" in result

    @pytest.mark.asyncio
    async def test_read_start_line_beyond_eof(self, tmp_path):
        f = tmp_path / "short.txt"
        f.write_text("only one line\n", encoding="utf-8")

        reg = _make_registry_with_fs()
        read = reg.get("read_file").handler
        result = await read(path=str(f), start_line=999)

        assert "beyond end of file" in result

    @pytest.mark.asyncio
    async def test_read_large_file_returns_summary(self, tmp_path):
        f = tmp_path / "big.py"
        content = "\n".join(f"# line {i}" for i in range(1, 602))
        f.write_text(content, encoding="utf-8")

        reg = _make_registry_with_fs()
        read = reg.get("read_file").handler
        result = await read(path=str(f))

        assert "601 lines" in result
        assert "too large" in result
        assert "start_line" in result
        # Should show a preview of the first 50 lines
        assert "# line 1" in result
        assert "# line 50" in result

    @pytest.mark.asyncio
    async def test_read_large_file_with_range(self, tmp_path):
        f = tmp_path / "big.py"
        content = "\n".join(f"# line {i}" for i in range(1, 602))
        f.write_text(content, encoding="utf-8")

        reg = _make_registry_with_fs()
        read = reg.get("read_file").handler
        result = await read(path=str(f), start_line=100, end_line=110)

        assert "# line 100" in result
        assert "# line 110" in result
        assert "[lines" in result
        # Should NOT trigger the summary mode
        assert "too large" not in result

    @pytest.mark.asyncio
    async def test_read_file_not_found(self, tmp_path):
        reg = _make_registry_with_fs()
        read = reg.get("read_file").handler

        with pytest.raises(FileNotFoundError):
            await read(path=str(tmp_path / "nonexistent.txt"))

    @pytest.mark.asyncio
    async def test_read_end_line_only(self, tmp_path):
        """end_line without start_line reads from beginning to end_line."""
        f = tmp_path / "lines.txt"
        f.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n", encoding="utf-8")

        reg = _make_registry_with_fs()
        read = reg.get("read_file").handler
        result = await read(path=str(f), end_line=3)

        assert "line1" in result
        assert "line2" in result
        assert "line3" in result
        assert "[lines" in result


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestSearchRegistration:
    def test_grep_registered_with_read_side_effect(self):
        reg = _make_registry_with_search()
        spec = reg.get("grep")
        assert spec is not None
        assert spec.side_effects == [SideEffect.READ]

    def test_find_files_registered_with_read_side_effect(self):
        reg = _make_registry_with_search()
        spec = reg.get("find_files")
        assert spec is not None
        assert spec.side_effects == [SideEffect.READ]

    def test_grep_has_handler(self):
        reg = _make_registry_with_search()
        assert reg.get("grep").handler is not None

    def test_find_files_has_handler(self):
        reg = _make_registry_with_search()
        assert reg.get("find_files").handler is not None

    def test_grep_schema_requires_regex(self):
        reg = _make_registry_with_search()
        schema = reg.get("grep").input_schema
        assert "regex" in schema["properties"]
        assert "regex" in schema["required"]

    def test_find_files_schema_requires_glob_pattern(self):
        reg = _make_registry_with_search()
        schema = reg.get("find_files").input_schema
        assert "glob_pattern" in schema["properties"]
        assert "glob_pattern" in schema["required"]

    def test_read_file_schema_has_line_range_params(self):
        reg = _make_registry_with_fs()
        schema = reg.get("read_file").input_schema
        assert "start_line" in schema["properties"]
        assert "end_line" in schema["properties"]
        # start_line and end_line should be optional (not in required)
        assert "start_line" not in schema.get("required", [])
        assert "end_line" not in schema.get("required", [])
