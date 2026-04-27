"""Tests for runtime provider switching."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.core.config import AgentConfig, ProviderConfig, load_config

# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


class TestResolveProvider:
    def test_inline_default(self):
        cfg = AgentConfig(provider=ProviderConfig(name="anthropic", model="claude-sonnet-4-6"))
        resolved = cfg.resolve_provider()
        assert resolved.name == "anthropic"
        assert resolved.model == "claude-sonnet-4-6"

    def test_by_string_key(self):
        cfg = AgentConfig(
            provider="gpt4",
            providers={
                "gpt4": ProviderConfig(name="openai", model="gpt-4o"),
            },
        )
        resolved = cfg.resolve_provider()
        assert resolved.name == "openai"
        assert resolved.model == "gpt-4o"

    def test_explicit_key_lookup(self):
        cfg = AgentConfig(
            providers={
                "local": ProviderConfig(name="ollama", model="llama3"),
            },
        )
        resolved = cfg.resolve_provider("local")
        assert resolved.name == "ollama"
        assert resolved.model == "llama3"

    def test_unknown_key_raises(self):
        cfg = AgentConfig(providers={})
        with pytest.raises(ValueError, match="Unknown provider key"):
            cfg.resolve_provider("nope")

    def test_string_key_not_in_dict_raises(self):
        cfg = AgentConfig(provider="missing", providers={})
        with pytest.raises(ValueError, match="not found in providers"):
            cfg.resolve_provider()

    def test_active_provider_key_string(self):
        cfg = AgentConfig(
            provider="claude",
            providers={"claude": ProviderConfig(name="anthropic", model="claude-sonnet-4-6")},
        )
        assert cfg.active_provider_key == "claude"

    def test_active_provider_key_inline(self):
        cfg = AgentConfig(provider=ProviderConfig())
        assert cfg.active_provider_key is None

    def test_backward_compat_inline(self):
        """Existing configs with inline ProviderConfig and no providers dict work."""
        cfg = AgentConfig(
            provider=ProviderConfig(name="anthropic", model="claude-sonnet-4-6"),
        )
        assert cfg.resolve_provider().name == "anthropic"
        assert cfg.providers == {}


# ---------------------------------------------------------------------------
# Per-provider effective_* overrides
# ---------------------------------------------------------------------------


class TestEffectiveOverrides:
    """Per-provider context_window / token_budget / cost_limit overrides."""

    def test_effective_context_window_from_provider(self):
        cfg = AgentConfig(
            provider="local",
            providers={
                "local": ProviderConfig(
                    name="ollama",
                    model="llama3",
                    context_window=32768,
                ),
            },
            context_window=200000,
        )
        assert cfg.effective_context_window() == 32768

    def test_effective_context_window_global_fallback(self):
        cfg = AgentConfig(
            provider="claude",
            providers={
                "claude": ProviderConfig(name="anthropic", model="claude-sonnet-4-6"),
            },
            context_window=200000,
        )
        assert cfg.effective_context_window() == 200000

    def test_effective_token_budget_from_provider(self):
        cfg = AgentConfig(
            provider="local",
            providers={
                "local": ProviderConfig(
                    name="ollama",
                    model="llama3",
                    token_budget=0,
                ),
            },
            token_budget=500000,
        )
        assert cfg.effective_token_budget() == 0

    def test_effective_token_budget_global_fallback(self):
        cfg = AgentConfig(
            provider=ProviderConfig(name="anthropic", model="claude-sonnet-4-6"),
            token_budget=500000,
        )
        assert cfg.effective_token_budget() == 500000

    def test_effective_cost_limit_from_provider(self):
        cfg = AgentConfig(
            provider="cloud",
            providers={
                "cloud": ProviderConfig(
                    name="openai",
                    model="gpt-4o",
                    cost_limit=10.0,
                ),
            },
            cost_limit=5.0,
        )
        assert cfg.effective_cost_limit() == 10.0

    def test_effective_cost_limit_global_fallback(self):
        cfg = AgentConfig(
            provider=ProviderConfig(name="anthropic", model="claude-sonnet-4-6"),
            cost_limit=5.0,
        )
        assert cfg.effective_cost_limit() == 5.0

    def test_effective_overrides_none_means_fallback(self):
        """When provider fields are None (default), global values are used."""
        cfg = AgentConfig(
            provider="x",
            providers={
                "x": ProviderConfig(
                    name="openai",
                    model="gpt-4o",
                    context_window=None,
                    token_budget=None,
                    cost_limit=None,
                ),
            },
            context_window=100000,
            token_budget=200000,
            cost_limit=3.0,
        )
        assert cfg.effective_context_window() == 100000
        assert cfg.effective_token_budget() == 200000
        assert cfg.effective_cost_limit() == 3.0

    def test_effective_zero_override_is_not_none(self):
        """Provider setting 0 explicitly overrides a non-zero global value."""
        cfg = AgentConfig(
            provider="local",
            providers={
                "local": ProviderConfig(
                    name="ollama",
                    model="llama3",
                    token_budget=0,
                    cost_limit=0.0,
                ),
            },
            token_budget=500000,
            cost_limit=5.0,
        )
        assert cfg.effective_token_budget() == 0
        assert cfg.effective_cost_limit() == 0.0


# ---------------------------------------------------------------------------
# Agent.switch_provider
# ---------------------------------------------------------------------------


def _mock_provider(config: ProviderConfig) -> MagicMock:
    """Return a lightweight mock provider for the given config."""
    p = MagicMock()
    p.name = config.name
    p.config = config
    p.supports_streaming = False
    p.supports_audio = False
    return p


class TestSwitchProvider:
    @patch("agent.core.agent._create_provider", side_effect=_mock_provider)
    def test_switch_by_registry_key(self, mock_create):
        from agent.core.agent import Agent

        cfg = AgentConfig(
            providers={
                "gpt4": ProviderConfig(name="openai", model="gpt-4o"),
            },
        )
        agent = Agent(config=cfg)
        desc = agent.switch_provider("gpt4")
        assert desc == "openai/gpt-4o"
        assert agent.provider.name == "openai"

    @patch("agent.core.agent._create_provider", side_effect=_mock_provider)
    def test_switch_by_slash_format(self, mock_create):
        from agent.core.agent import Agent

        agent = Agent(config=AgentConfig())
        desc = agent.switch_provider("ollama/llama3")
        assert desc == "ollama/llama3"
        assert agent.provider.name == "ollama"

    @patch("agent.core.agent._create_provider", side_effect=_mock_provider)
    def test_switch_unknown_key_raises(self, mock_create):
        from agent.core.agent import Agent

        agent = Agent(config=AgentConfig())
        with pytest.raises(ValueError, match="not a known provider key"):
            agent.switch_provider("nope")

    @patch("agent.core.agent._create_provider", side_effect=_mock_provider)
    def test_switch_preserves_session(self, mock_create):
        from agent.core.agent import Agent
        from agent.core.session import Session

        agent = Agent(config=AgentConfig())
        session = Session()
        session.add_user_message("hello")
        assert len(session.events) >= 1

        agent.switch_provider("openai/gpt-4o")
        # Session is independent — not cleared by switch
        assert len(session.events) >= 1


# ---------------------------------------------------------------------------
# Config loading (JSON round-trip)
# ---------------------------------------------------------------------------


class TestConfigLoading:
    def test_load_with_providers_dict(self, tmp_path: Path):
        data = {
            "provider": "claude",
            "providers": {
                "claude": {"name": "anthropic", "model": "claude-sonnet-4-6"},
                "gpt4": {"name": "openai", "model": "gpt-4o"},
            },
        }
        p = tmp_path / "config.json"
        p.write_text(json.dumps(data))

        cfg = load_config(p)
        assert cfg.provider == "claude"
        assert "gpt4" in cfg.providers
        assert cfg.resolve_provider().name == "anthropic"

    def test_load_inline_provider(self, tmp_path: Path):
        """Legacy configs with inline provider object still load."""
        data = {
            "provider": {"name": "anthropic", "model": "claude-sonnet-4-6"},
        }
        p = tmp_path / "config.json"
        p.write_text(json.dumps(data))

        cfg = load_config(p)
        assert isinstance(cfg.provider, ProviderConfig)
        assert cfg.resolve_provider().name == "anthropic"
