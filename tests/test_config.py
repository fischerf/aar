"""Config tests — system prompt assembly from rules files."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.core.config import (
    AgentConfig,
    _default_system_prompt,
    build_system_prompt,
)


# ---------------------------------------------------------------------------
# _default_system_prompt
# ---------------------------------------------------------------------------


class TestDefaultSystemPrompt:
    def test_contains_os_and_cwd(self):
        prompt = _default_system_prompt()
        assert "Operating system:" in prompt
        assert "Working directory:" in prompt

    def test_contains_assistant_preamble(self):
        prompt = _default_system_prompt()
        assert "You are a helpful assistant" in prompt


# ---------------------------------------------------------------------------
# build_system_prompt — rules file layering
# ---------------------------------------------------------------------------


class TestBuildSystemPrompt:
    def test_base_only_when_no_rules_files(self, tmp_path, monkeypatch):
        """With no rules files present, output equals the base prompt."""
        monkeypatch.chdir(tmp_path)
        # Ensure neither global nor project rules exist
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))
        prompt = build_system_prompt()
        assert prompt == _default_system_prompt()

    def test_global_rules_appended(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        fake_home = tmp_path / "fakehome"
        (fake_home / ".aar").mkdir(parents=True)
        (fake_home / ".aar" / "rules.md").write_text("global rule A", encoding="utf-8")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        prompt = build_system_prompt()
        assert "global rule A" in prompt
        assert prompt.index("Operating system:") < prompt.index("global rule A")

    def test_project_rules_appended(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".agent").mkdir()
        (tmp_path / ".agent" / "rules.md").write_text("project rule B", encoding="utf-8")
        # No global rules
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))

        prompt = build_system_prompt()
        assert "project rule B" in prompt

    def test_both_rules_layered_in_order(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        fake_home = tmp_path / "fakehome"
        (fake_home / ".aar").mkdir(parents=True)
        (fake_home / ".aar" / "rules.md").write_text("global rule", encoding="utf-8")
        (tmp_path / ".agent").mkdir()
        (tmp_path / ".agent" / "rules.md").write_text("project rule", encoding="utf-8")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        prompt = build_system_prompt()
        # All three sections present, separated by ---
        assert "---" in prompt
        assert prompt.index("global rule") < prompt.index("project rule")

    def test_separator_is_dashes(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        fake_home = tmp_path / "fakehome"
        (fake_home / ".aar").mkdir(parents=True)
        (fake_home / ".aar" / "rules.md").write_text("g", encoding="utf-8")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        prompt = build_system_prompt()
        assert "\n---\n" in prompt

    def test_whitespace_stripped_from_rules(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".agent").mkdir()
        (tmp_path / ".agent" / "rules.md").write_text(
            "\n\n  padded content  \n\n", encoding="utf-8"
        )
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))

        prompt = build_system_prompt()
        assert "padded content" in prompt
        # The rules section itself should be stripped
        assert "\n\n  padded content  \n\n" not in prompt

    def test_missing_agent_dir_no_error(self, tmp_path, monkeypatch):
        """No .agent/ directory at all — should not raise."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))
        prompt = build_system_prompt()
        assert "Operating system:" in prompt


# ---------------------------------------------------------------------------
# AgentConfig integration
# ---------------------------------------------------------------------------


class TestAgentConfigSystemPrompt:
    def test_default_uses_build_system_prompt(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".agent").mkdir()
        (tmp_path / ".agent" / "rules.md").write_text("from config test", encoding="utf-8")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))

        config = AgentConfig()
        assert "from config test" in config.system_prompt

    def test_explicit_system_prompt_overrides(self):
        config = AgentConfig(system_prompt="custom prompt only")
        assert config.system_prompt == "custom prompt only"
        assert "Operating system:" not in config.system_prompt
