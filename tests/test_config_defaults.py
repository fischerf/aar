"""Verify that all configuration defaults originate from config.py models.

The single source of truth for every default value is the Pydantic models in
``agent.core.config``.  The CLI commands (chat, run, tui, serve) and ``aar init``
must never introduce their own defaults — they should either pass ``None`` (let
the model decide) or derive values from ``AgentConfig()``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.core.config import (
    AgentConfig,
    ProviderConfig,
    SafetyConfig,
    ToolConfig,
    load_config,
)
import agent.transports.cli as cli_mod
from agent.transports.cli import _build_config


# ---------------------------------------------------------------------------
# Canonical defaults — snapshot from the Pydantic models
# ---------------------------------------------------------------------------

_CANONICAL = AgentConfig()


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """Redirect _USER_DIR / _USER_CONFIG / _USER_MCP_CONFIG to a temp dir."""
    fake = tmp_path / "home"
    aar_dir = fake / ".aar"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake))
    monkeypatch.setattr(cli_mod, "_USER_DIR", aar_dir)
    monkeypatch.setattr(cli_mod, "_USER_CONFIG", aar_dir / "config.json")
    monkeypatch.setattr(cli_mod, "_USER_MCP_CONFIG", aar_dir / "mcp_servers.json")
    return fake


# ---------------------------------------------------------------------------
# _build_config: no args ⇒ pure AgentConfig() defaults
# ---------------------------------------------------------------------------


class TestBuildConfigDefaults:
    """_build_config() with no arguments must equal AgentConfig() defaults."""

    def test_provider_name(self, fake_home):
        cfg = _build_config()
        assert cfg.provider.name == _CANONICAL.provider.name

    def test_provider_model(self, fake_home):
        cfg = _build_config()
        assert cfg.provider.model == _CANONICAL.provider.model

    def test_provider_base_url(self, fake_home):
        cfg = _build_config()
        assert cfg.provider.base_url == _CANONICAL.provider.base_url

    def test_provider_max_tokens(self, fake_home):
        cfg = _build_config()
        assert cfg.provider.max_tokens == _CANONICAL.provider.max_tokens

    def test_provider_temperature(self, fake_home):
        cfg = _build_config()
        assert cfg.provider.temperature == _CANONICAL.provider.temperature

    def test_max_steps(self, fake_home):
        cfg = _build_config()
        assert cfg.max_steps == _CANONICAL.max_steps

    def test_max_tokens_per_turn(self, fake_home):
        cfg = _build_config()
        assert cfg.max_tokens_per_turn == _CANONICAL.max_tokens_per_turn

    def test_timeout(self, fake_home):
        cfg = _build_config()
        assert cfg.timeout == _CANONICAL.timeout

    def test_safety_read_only(self, fake_home):
        cfg = _build_config()
        assert cfg.safety.read_only == _CANONICAL.safety.read_only

    def test_safety_require_approval_writes(self, fake_home):
        cfg = _build_config()
        assert cfg.safety.require_approval_for_writes == _CANONICAL.safety.require_approval_for_writes

    def test_safety_require_approval_execute(self, fake_home):
        cfg = _build_config()
        assert cfg.safety.require_approval_for_execute == _CANONICAL.safety.require_approval_for_execute

    def test_safety_denied_paths(self, fake_home):
        cfg = _build_config()
        assert cfg.safety.denied_paths == _CANONICAL.safety.denied_paths

    def test_safety_sandbox(self, fake_home):
        cfg = _build_config()
        assert cfg.safety.sandbox == _CANONICAL.safety.sandbox

    def test_tools_enabled_builtins(self, fake_home):
        cfg = _build_config()
        assert cfg.tools.enabled_builtins == _CANONICAL.tools.enabled_builtins

    def test_tools_command_timeout(self, fake_home):
        cfg = _build_config()
        assert cfg.tools.command_timeout == _CANONICAL.tools.command_timeout


# ---------------------------------------------------------------------------
# Config file respects: loaded values are NOT overwritten by CLI defaults
# ---------------------------------------------------------------------------


class TestConfigFileRespected:
    """When a config file provides non-default values, _build_config must not
    overwrite them with CLI defaults (all CLI options default to None)."""

    @pytest.fixture()
    def custom_config_file(self, tmp_path):
        data = {
            "provider": {
                "name": "ollama",
                "model": "llama3.2",
                "base_url": "http://localhost:11434",
                "max_tokens": 2048,
            },
            "max_steps": 10,
            "safety": {
                "read_only": True,
                "require_approval_for_writes": True,
                "require_approval_for_execute": True,
            },
        }
        path = tmp_path / "custom.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def test_provider_preserved(self, custom_config_file):
        cfg = _build_config(config_file=custom_config_file)
        assert cfg.provider.name == "ollama"
        assert cfg.provider.model == "llama3.2"
        assert cfg.provider.base_url == "http://localhost:11434"
        assert cfg.provider.max_tokens == 2048

    def test_max_steps_preserved(self, custom_config_file):
        cfg = _build_config(config_file=custom_config_file)
        assert cfg.max_steps == 10

    def test_safety_preserved(self, custom_config_file):
        cfg = _build_config(config_file=custom_config_file)
        assert cfg.safety.read_only is True
        assert cfg.safety.require_approval_for_writes is True
        assert cfg.safety.require_approval_for_execute is True

    def test_cli_flag_overrides_config_file(self, custom_config_file):
        cfg = _build_config(
            config_file=custom_config_file,
            model="gpt-4",
            provider="openai",
            max_steps=99,
            read_only=False,
        )
        assert cfg.provider.name == "openai"
        assert cfg.provider.model == "gpt-4"
        assert cfg.max_steps == 99
        assert cfg.safety.read_only is False


# ---------------------------------------------------------------------------
# Auto-loaded ~/.aar/config.json respects same precedence
# ---------------------------------------------------------------------------


class TestUserConfigAutoLoad:
    """~/.aar/config.json is loaded automatically and not overwritten."""

    def test_auto_loads_user_config(self, fake_home):
        aar_dir = fake_home / ".aar"
        aar_dir.mkdir(parents=True)
        data = {"provider": {"name": "ollama", "model": "mistral"}, "max_steps": 7}
        (aar_dir / "config.json").write_text(json.dumps(data), encoding="utf-8")

        cfg = _build_config()
        assert cfg.provider.name == "ollama"
        assert cfg.provider.model == "mistral"
        assert cfg.max_steps == 7

    def test_explicit_config_overrides_user_config(self, fake_home, tmp_path):
        # User config exists but --config points elsewhere
        aar_dir = fake_home / ".aar"
        aar_dir.mkdir(parents=True)
        (aar_dir / "config.json").write_text(
            json.dumps({"provider": {"name": "ollama"}}), encoding="utf-8"
        )

        explicit = tmp_path / "explicit.json"
        explicit.write_text(json.dumps({"provider": {"name": "openai"}}), encoding="utf-8")

        cfg = _build_config(config_file=str(explicit))
        assert cfg.provider.name == "openai"


# ---------------------------------------------------------------------------
# aar init: generated config.json matches AgentConfig() defaults
# ---------------------------------------------------------------------------


class TestInitConfigMatchesDefaults:
    """The config.json written by ``aar init`` must round-trip back to the
    same values as ``AgentConfig()``."""

    def test_init_config_roundtrips_to_defaults(self, fake_home):
        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(cli_mod.app, ["init"])
        assert result.exit_code == 0

        config_path = fake_home / ".aar" / "config.json"
        assert config_path.is_file()

        loaded = load_config(config_path)
        canonical = AgentConfig()

        # Compare all fields except system_prompt (runtime-dependent)
        assert loaded.provider.name == canonical.provider.name
        assert loaded.provider.model == canonical.provider.model
        assert loaded.provider.api_key == canonical.provider.api_key
        assert loaded.provider.base_url == canonical.provider.base_url
        assert loaded.provider.max_tokens == canonical.provider.max_tokens
        assert loaded.provider.temperature == canonical.provider.temperature
        assert loaded.provider.extra == canonical.provider.extra
        assert loaded.tools.enabled_builtins == canonical.tools.enabled_builtins
        assert loaded.tools.allowed_paths == canonical.tools.allowed_paths
        assert loaded.tools.command_timeout == canonical.tools.command_timeout
        assert loaded.tools.max_output_chars == canonical.tools.max_output_chars
        assert loaded.safety.read_only == canonical.safety.read_only
        assert loaded.safety.require_approval_for_writes == canonical.safety.require_approval_for_writes
        assert loaded.safety.require_approval_for_execute == canonical.safety.require_approval_for_execute
        assert loaded.safety.denied_paths == canonical.safety.denied_paths
        assert loaded.safety.allowed_paths == canonical.safety.allowed_paths
        assert loaded.safety.sandbox == canonical.safety.sandbox
        assert loaded.safety.sandbox_max_memory_mb == canonical.safety.sandbox_max_memory_mb
        assert loaded.safety.log_all_commands == canonical.safety.log_all_commands
        assert loaded.max_steps == canonical.max_steps
        assert loaded.max_tokens_per_turn == canonical.max_tokens_per_turn
        assert loaded.timeout == canonical.timeout

    def test_init_creates_mcp_example(self, fake_home):
        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(cli_mod.app, ["init"])
        assert result.exit_code == 0

        mcp_path = fake_home / ".aar" / "mcp_servers.json"
        mcp_example = fake_home / ".aar" / "mcp_servers.example.json"
        assert mcp_path.is_file()
        assert mcp_example.is_file()

        # mcp_servers.json must be empty (works out of the box)
        mcp_data = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert mcp_data == {"servers": []}

        # example must have server entries
        example_data = json.loads(mcp_example.read_text(encoding="utf-8"))
        assert len(example_data["servers"]) > 0

    def test_init_warns_on_existing(self, fake_home):
        from typer.testing import CliRunner

        runner = CliRunner()
        # First run creates files
        runner.invoke(cli_mod.app, ["init"])
        # Second run warns
        result = runner.invoke(cli_mod.app, ["init"])
        assert result.exit_code == 0
        assert "already exists" in result.output

    def test_init_force_overwrites(self, fake_home):
        from typer.testing import CliRunner

        runner = CliRunner()
        runner.invoke(cli_mod.app, ["init"])

        # Corrupt the config
        config_path = fake_home / ".aar" / "config.json"
        config_path.write_text('{"provider": {"name": "corrupted"}}', encoding="utf-8")

        # Force overwrites
        result = runner.invoke(cli_mod.app, ["init", "--force"])
        assert result.exit_code == 0

        loaded = load_config(config_path)
        assert loaded.provider.name == AgentConfig().provider.name
