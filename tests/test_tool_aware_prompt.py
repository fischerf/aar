"""Phase 1 tests — tool-aware system prompt: snippets, guidelines, and prompt assembly."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.core.config import build_system_prompt
from agent.tools.registry import ToolRegistry
from agent.tools.schema import SideEffect, ToolSpec

# ---------------------------------------------------------------------------
# ToolSpec — prompt_snippet and prompt_guidelines defaults
# ---------------------------------------------------------------------------


class TestToolSpecPromptFields:
    def test_default_snippet_is_empty(self):
        spec = ToolSpec(name="t", description="d")
        assert spec.prompt_snippet == ""

    def test_default_guidelines_is_empty_list(self):
        spec = ToolSpec(name="t", description="d")
        assert spec.prompt_guidelines == []

    def test_snippet_roundtrips(self):
        spec = ToolSpec(name="t", description="d", prompt_snippet="Do something")
        assert spec.prompt_snippet == "Do something"

    def test_guidelines_roundtrips(self):
        spec = ToolSpec(name="t", description="d", prompt_guidelines=["g1", "g2"])
        assert spec.prompt_guidelines == ["g1", "g2"]

    def test_snippet_excluded_from_provider_schema(self):
        spec = ToolSpec(name="t", description="d", prompt_snippet="snip")
        schema = spec.to_provider_schema()
        assert "prompt_snippet" not in schema
        assert schema == {"name": "t", "description": "d", "input_schema": {}}


# ---------------------------------------------------------------------------
# ToolRegistry — get_prompt_snippets / get_prompt_guidelines
# ---------------------------------------------------------------------------


class TestRegistryPromptHarvesting:
    def _make_registry(self) -> ToolRegistry:
        reg = ToolRegistry()
        reg.add(
            ToolSpec(
                name="read_file",
                description="Read a file",
                prompt_snippet="Read file contents",
            )
        )
        reg.add(
            ToolSpec(
                name="write_file",
                description="Write a file",
                prompt_snippet="Create or overwrite a file",
            )
        )
        reg.add(
            ToolSpec(
                name="bash",
                description="Run a command",
                # no prompt_snippet — should be excluded from snippets
            )
        )
        reg.add(
            ToolSpec(
                name="grep",
                description="Search",
                prompt_snippet="Search file contents with regex",
                prompt_guidelines=[
                    "Use grep for content, find_files for paths.",
                    "Prefer grep over bash for searching.",
                ],
            )
        )
        reg.add(
            ToolSpec(
                name="find_files",
                description="Find files",
                prompt_snippet="Find files by glob",
                prompt_guidelines=[
                    "Use grep for content, find_files for paths.",  # duplicate of grep's
                ],
            )
        )
        return reg

    def test_get_prompt_snippets_excludes_empty(self):
        reg = self._make_registry()
        snippets = reg.get_prompt_snippets()
        assert "bash" not in snippets
        assert len(snippets) == 4  # read_file, write_file, grep, find_files

    def test_get_prompt_snippets_values(self):
        reg = self._make_registry()
        snippets = reg.get_prompt_snippets()
        assert snippets["read_file"] == "Read file contents"
        assert snippets["grep"] == "Search file contents with regex"

    def test_get_prompt_guidelines_deduplicates(self):
        reg = self._make_registry()
        guidelines = reg.get_prompt_guidelines()
        # "Use grep for content, find_files for paths." appears in both grep and find_files
        assert guidelines.count("Use grep for content, find_files for paths.") == 1

    def test_get_prompt_guidelines_preserves_order(self):
        reg = self._make_registry()
        guidelines = reg.get_prompt_guidelines()
        assert len(guidelines) == 2
        assert guidelines[0] == "Use grep for content, find_files for paths."
        assert guidelines[1] == "Prefer grep over bash for searching."

    def test_empty_registry_returns_empty(self):
        reg = ToolRegistry()
        assert reg.get_prompt_snippets() == {}
        assert reg.get_prompt_guidelines() == []


# ---------------------------------------------------------------------------
# build_system_prompt — tool snippets and guidelines integration
# ---------------------------------------------------------------------------


class TestBuildSystemPromptWithTools:
    def test_no_tools_section_when_none_provided(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))
        prompt = build_system_prompt(project_rules_dir=tmp_path / "norules")
        assert "Available tools:" not in prompt

    def test_tools_section_with_snippets(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))
        snippets = {"read_file": "Read a file", "bash": "Run shell commands"}
        prompt = build_system_prompt(
            project_rules_dir=tmp_path / "norules",
            tool_snippets=snippets,
        )
        assert "Available tools:" in prompt
        assert "- read_file: Read a file" in prompt
        assert "- bash: Run shell commands" in prompt

    def test_tools_section_with_guidelines(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))
        guidelines = ["Prefer grep over bash for searching."]
        prompt = build_system_prompt(
            project_rules_dir=tmp_path / "norules",
            tool_guidelines=guidelines,
        )
        assert "Guidelines:" in prompt
        assert "- Prefer grep over bash for searching." in prompt

    def test_tools_section_with_both(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))
        snippets = {"grep": "Search contents"}
        guidelines = ["Search before assuming."]
        prompt = build_system_prompt(
            project_rules_dir=tmp_path / "norules",
            tool_snippets=snippets,
            tool_guidelines=guidelines,
        )
        assert "Available tools:" in prompt
        assert "- grep: Search contents" in prompt
        assert "Guidelines:" in prompt
        assert "- Search before assuming." in prompt

    def test_tools_layer_comes_after_base(self, tmp_path, monkeypatch):
        """Tools section should appear between the base prompt and any rules."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))
        # Create a project rules file
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "rules.md").write_text("Project rules here.", encoding="utf-8")
        snippets = {"bash": "Shell"}
        prompt = build_system_prompt(
            project_rules_dir=rules_dir,
            tool_snippets=snippets,
        )
        base_pos = prompt.find("You are a helpful assistant")
        tools_pos = prompt.find("Available tools:")
        rules_pos = prompt.find("Project rules here.")
        assert base_pos < tools_pos < rules_pos

    def test_empty_snippets_dict_no_tools_section(self, tmp_path, monkeypatch):
        """Passing an empty dict should not add a tools section."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))
        prompt = build_system_prompt(
            project_rules_dir=tmp_path / "norules",
            tool_snippets={},
            tool_guidelines=[],
        )
        assert "Available tools:" not in prompt


# ---------------------------------------------------------------------------
# Built-in tools — verify snippets are populated
# ---------------------------------------------------------------------------


class TestBuiltinToolSnippets:
    def test_filesystem_tools_have_snippets(self):
        from agent.tools.builtin.filesystem import register_filesystem_tools

        reg = ToolRegistry()
        register_filesystem_tools(reg)
        for name in ["read_file", "write_file", "edit_file", "list_directory"]:
            spec = reg.get(name)
            assert spec is not None, f"{name} not registered"
            assert spec.prompt_snippet, f"{name} has no prompt_snippet"

    def test_shell_tool_has_snippet(self):
        from agent.tools.builtin.shell import register_shell_tools

        reg = ToolRegistry()
        register_shell_tools(reg)
        spec = reg.get("bash")
        assert spec is not None
        assert spec.prompt_snippet

    def test_search_tools_have_snippets(self):
        from agent.tools.builtin.search import register_search_tools

        reg = ToolRegistry()
        register_search_tools(reg)
        for name in ["grep", "find_files"]:
            spec = reg.get(name)
            assert spec is not None, f"{name} not registered"
            assert spec.prompt_snippet, f"{name} has no prompt_snippet"

    def test_grep_has_guidelines(self):
        from agent.tools.builtin.search import register_search_tools

        reg = ToolRegistry()
        register_search_tools(reg)
        spec = reg.get("grep")
        assert spec is not None
        assert len(spec.prompt_guidelines) > 0

    def test_all_builtins_produce_prompt_snippets(self):
        """End-to-end: register all builtins, verify snippets harvesting."""
        from agent.tools.builtin.filesystem import register_filesystem_tools
        from agent.tools.builtin.search import register_search_tools
        from agent.tools.builtin.shell import register_shell_tools

        reg = ToolRegistry()
        register_filesystem_tools(reg)
        register_shell_tools(reg)
        register_search_tools(reg)

        snippets = reg.get_prompt_snippets()
        expected = {
            "read_file",
            "write_file",
            "edit_file",
            "list_directory",
            "bash",
            "grep",
            "find_files",
        }
        assert set(snippets.keys()) == expected

    def test_all_builtins_guidelines_are_harvested(self):
        from agent.tools.builtin.filesystem import register_filesystem_tools
        from agent.tools.builtin.search import register_search_tools
        from agent.tools.builtin.shell import register_shell_tools

        reg = ToolRegistry()
        register_filesystem_tools(reg)
        register_shell_tools(reg)
        register_search_tools(reg)

        guidelines = reg.get_prompt_guidelines()
        assert len(guidelines) >= 1
        assert any("grep" in g for g in guidelines)
