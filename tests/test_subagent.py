"""Tests for the built-in ``spawn_agent`` tool."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from agent.core.agent import Agent
from agent.core.config import AgentConfig, SubAgentConfig, SubAgentProfile
from agent.core.events import EventType, SubAgentEvent
from agent.tools.builtin.subagent import TOOL_NAME, build_child_config, register_subagent_tool


def _config(**subagents) -> AgentConfig:
    base = {
        "enabled": True,
        "max_depth": 1,
        "agents": {
            "researcher": SubAgentProfile(
                description="Reads code",
                tools=["read_file", "grep"],
                system_prompt="You only read.",
                max_steps=5,
                timeout=30,
            )
        },
    }
    base.update(subagents)
    return AgentConfig(subagents=SubAgentConfig(**base))


def _agent(config: AgentConfig) -> Agent:
    return Agent(config, provider=MagicMock())


# ---------------------------------------------------------------------------
# Registration — the model must not see a tool that can only error
# ---------------------------------------------------------------------------


def test_not_registered_when_disabled():
    agent = _agent(_config(enabled=False))
    assert agent.registry.get(TOOL_NAME) is None


def test_not_registered_without_profiles():
    agent = _agent(_config(agents={}))
    assert agent.registry.get(TOOL_NAME) is None


def test_not_registered_at_zero_depth():
    """A leaf agent has no spawn_agent to call — that is the recursion guard."""
    agent = _agent(_config(max_depth=0))
    assert agent.registry.get(TOOL_NAME) is None


def test_registered_when_configured():
    spec = _agent(_config()).registry.get(TOOL_NAME)
    assert spec is not None
    assert spec.input_schema["properties"]["agent_name"]["enum"] == ["researcher"]
    # The profile timeout must survive the executor's shared command_timeout.
    assert spec.timeout_s == 30


def test_description_lists_the_profiles():
    spec = _agent(_config()).registry.get(TOOL_NAME)
    assert "researcher: Reads code" in spec.description


# ---------------------------------------------------------------------------
# Child config — the model chooses a name and a task, nothing else
# ---------------------------------------------------------------------------


def test_child_inherits_safety_verbatim():
    parent = _config()
    parent.safety.sandbox.mode = "wsl"
    parent.safety.denied_paths = ["/etc/**"]
    child = build_child_config(parent, parent.subagents.agents["researcher"], 0)
    assert child.safety.sandbox.mode == "wsl"
    assert child.safety.denied_paths == ["/etc/**"]


def test_child_tools_are_intersected_with_the_parent():
    """A sub-agent is never more capable than the agent that spawned it."""
    parent = _config()
    parent.tools.enabled_builtins = ["read_file"]  # no grep
    child = build_child_config(parent, parent.subagents.agents["researcher"], 0)
    assert child.tools.enabled_builtins == ["read_file"]


def test_child_extension_tools_default_to_inheriting_everything():
    parent = _config()
    child = build_child_config(parent, parent.subagents.agents["researcher"], 0)
    assert child.tools.enabled_extension_tools is None


def test_child_extension_allowlist_is_passed_down():
    parent = _config()
    parent.subagents.agents["researcher"].extension_tools = ["image_generate"]
    child = build_child_config(parent, parent.subagents.agents["researcher"], 0)
    assert child.tools.enabled_extension_tools == ["image_generate"]


def test_child_depth_budget_decrements():
    parent = _config(max_depth=2)
    child = build_child_config(parent, parent.subagents.agents["researcher"], 1)
    assert child.subagents.max_depth == 1


def test_child_system_prompt_is_actually_applied():
    parent = _config()
    child_cfg = build_child_config(parent, parent.subagents.agents["researcher"], 0)
    child = Agent(child_cfg, provider=MagicMock())
    assert child.config.system_prompt == "You only read."


def test_child_provider_override_resolves_from_the_parent_registry():
    from agent.core.config import ProviderConfig

    parent = _config()
    parent.providers = {"fast": ProviderConfig(name="ollama", model="qwen3.8")}
    parent.subagents.agents["researcher"].provider = "fast"
    child = build_child_config(parent, parent.subagents.agents["researcher"], 0)
    assert child.resolve_provider().model == "qwen3.8"


def test_unknown_provider_key_is_rejected():
    parent = _config()
    parent.subagents.agents["researcher"].provider = "nope"
    with pytest.raises(ValueError, match="Unknown provider key"):
        build_child_config(parent, parent.subagents.agents["researcher"], 0)


# ---------------------------------------------------------------------------
# Running one
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_returns_the_child_final_message(tmp_path, monkeypatch):
    parent = _agent(_config())
    parent.config.session_dir = tmp_path / "sessions"

    async def fake_chat(self, task, session=None):
        from agent.core.session import Session

        self.last_session = Session()
        return f"answer to: {task}"

    monkeypatch.setattr(Agent, "chat", fake_chat)

    out = await parent.registry.get(TOOL_NAME).handler(agent_name="researcher", task="what is X")
    assert out.startswith("answer to: what is X")
    assert "[sub-agent researcher:" in out


@pytest.mark.asyncio
async def test_unknown_agent_name_is_an_error_string(tmp_path):
    parent = _agent(_config())
    out = await parent.registry.get(TOOL_NAME).handler(agent_name="ghost", task="x")
    assert "unknown agent 'ghost'" in out
    assert "researcher" in out


@pytest.mark.asyncio
async def test_child_failure_is_reported_not_raised(monkeypatch, tmp_path):
    parent = _agent(_config())
    parent.config.session_dir = tmp_path / "sessions"

    async def boom(self, task, session=None):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(Agent, "chat", boom)

    out = await parent.registry.get(TOOL_NAME).handler(agent_name="researcher", task="x")
    assert out.startswith("Error: sub-agent 'researcher' failed")
    assert "provider exploded" in out


@pytest.mark.asyncio
async def test_child_timeout_is_reported(monkeypatch, tmp_path):
    cfg = _config()
    cfg.subagents.agents["researcher"].timeout = 1
    parent = _agent(cfg)
    parent.config.session_dir = tmp_path / "sessions"

    async def hang(self, task, session=None):
        await asyncio.sleep(60)

    monkeypatch.setattr(Agent, "chat", hang)

    out = await parent.registry.get(TOOL_NAME).handler(agent_name="researcher", task="x")
    assert "timed out after 1s" in out


@pytest.mark.asyncio
async def test_spawn_emits_events_on_the_parent_session(monkeypatch, tmp_path):
    from agent.core.session import Session

    parent = _agent(_config())
    parent.config.session_dir = tmp_path / "sessions"
    parent.last_session = Session()

    async def fake_chat(self, task, session=None):
        self.last_session = Session()
        return "done"

    monkeypatch.setattr(Agent, "chat", fake_chat)
    await parent.registry.get(TOOL_NAME).handler(agent_name="researcher", task="x")

    events = [e for e in parent.last_session.events if isinstance(e, SubAgentEvent)]
    assert [e.status for e in events] == ["started", "completed"]
    assert events[0].type == EventType.SUBAGENT
    assert events[1].duration_ms is not None


@pytest.mark.asyncio
async def test_child_session_is_persisted(monkeypatch, tmp_path):
    from agent.core.session import Session

    parent = _agent(_config())
    parent.config.session_dir = tmp_path / "sessions"
    child_session = Session()

    async def fake_chat(self, task, session=None):
        self.last_session = child_session
        return "done"

    monkeypatch.setattr(Agent, "chat", fake_chat)
    await parent.registry.get(TOOL_NAME).handler(agent_name="researcher", task="x")

    from agent.memory.session_store import SessionStore

    assert SessionStore(tmp_path / "sessions").load(child_session.session_id) is not None


# ---------------------------------------------------------------------------
# Recursion
# ---------------------------------------------------------------------------


def test_a_child_at_the_last_level_cannot_spawn():
    parent = _config(max_depth=1)
    child_cfg = build_child_config(parent, parent.subagents.agents["researcher"], 0)
    child = Agent(child_cfg, provider=MagicMock())
    assert child.registry.get(TOOL_NAME) is None


def test_a_child_with_budget_left_can_spawn():
    parent = _config(max_depth=2)
    child_cfg = build_child_config(parent, parent.subagents.agents["researcher"], 1)
    child = Agent(child_cfg, provider=MagicMock())
    assert child.registry.get(TOOL_NAME) is not None


def test_register_returns_false_when_nothing_was_added():
    from agent.tools.registry import ToolRegistry

    agent = _agent(_config(enabled=False))
    assert register_subagent_tool(ToolRegistry(), parent=agent) is False
