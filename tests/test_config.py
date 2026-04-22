"""Config tests — system prompt assembly from rules files."""

from __future__ import annotations

from pathlib import Path


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

    def test_contains_shell_line(self):
        prompt = _default_system_prompt()
        assert "Shell:" in prompt

    def test_no_git_bash_mention(self):
        # Git Bash is no longer referenced; WSL is the Windows shell
        prompt = _default_system_prompt()
        assert "Git Bash" not in prompt


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


# ---------------------------------------------------------------------------
# build_system_prompt — project_rules_dir override
# ---------------------------------------------------------------------------


class TestBuildSystemPromptProjectRulesDir:
    def test_custom_rules_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))
        custom_dir = Path(".config/aar")
        (tmp_path / custom_dir).mkdir(parents=True)
        (tmp_path / custom_dir / "rules.md").write_text("custom dir rule", encoding="utf-8")

        prompt = build_system_prompt(project_rules_dir=custom_dir)
        assert "custom dir rule" in prompt

    def test_custom_rules_dir_ignores_default(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))
        # Create rules in the default location — should be ignored
        (tmp_path / ".agent").mkdir()
        (tmp_path / ".agent" / "rules.md").write_text("default rule", encoding="utf-8")

        prompt = build_system_prompt(project_rules_dir=Path(".custom"))
        assert "default rule" not in prompt

    def test_none_falls_back_to_dot_agent(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))
        (tmp_path / ".agent").mkdir()
        (tmp_path / ".agent" / "rules.md").write_text("default rule", encoding="utf-8")

        prompt = build_system_prompt(project_rules_dir=None)
        assert "default rule" in prompt


# ---------------------------------------------------------------------------
# AgentConfig — sandbox config and project_rules_dir integration
# ---------------------------------------------------------------------------


class TestAgentConfigNewFields:
    def test_sandbox_mode_default_is_local(self):
        config = AgentConfig()
        assert config.safety.sandbox.mode == "local"

    def test_project_rules_dir_default_is_dot_agent(self):
        config = AgentConfig()
        assert config.project_rules_dir == Path(".agent")

    def test_custom_project_rules_dir_used_in_prompt(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))
        custom_dir = Path(".myagent")
        (tmp_path / custom_dir).mkdir()
        (tmp_path / custom_dir / "rules.md").write_text("my custom rule", encoding="utf-8")

        config = AgentConfig(project_rules_dir=custom_dir)
        assert "my custom rule" in config.system_prompt

    def test_explicit_system_prompt_overrides(self):
        config = AgentConfig(system_prompt="explicit override")
        assert config.system_prompt == "explicit override"

    def test_session_dir_default_unchanged(self):
        config = AgentConfig()
        assert config.session_dir == Path(".agent/sessions")

    def test_load_config_with_new_fields(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            '{"safety": {"sandbox": {"mode": "linux"}}, "project_rules_dir": ".myconfig"}',
            encoding="utf-8",
        )
        from agent.core.config import load_config

        config = load_config(cfg_file)
        assert config.safety.sandbox.mode == "linux"
        assert config.project_rules_dir == Path(".myconfig")


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


class TestAgentConfigTimeoutValidation:
    def test_acp_approval_timeout_exceeds_loop_timeout_is_valid(self):
        # No error at config level — only a warning at ACP transport startup
        cfg = AgentConfig(timeout=10.0, safety={"acp_approval_timeout": 30.0})
        assert cfg.safety.acp_approval_timeout == 30.0

    def test_acp_approval_timeout_equals_loop_timeout_is_ok(self):
        cfg = AgentConfig(timeout=30.0, safety={"acp_approval_timeout": 30.0})
        assert cfg.safety.acp_approval_timeout == 30.0

    def test_loop_timeout_zero_is_valid(self):
        cfg = AgentConfig(timeout=0.0, safety={"acp_approval_timeout": 999.0})
        assert cfg.safety.acp_approval_timeout == 999.0

    def test_acp_approval_timeout_zero_is_valid(self):
        # Default config — conflict is warned at ACP transport startup only
        cfg = AgentConfig(timeout=5.0, safety={"acp_approval_timeout": 0.0})
        assert cfg.timeout == 5.0
