"""Built-in ``spawn_agent`` tool — run a nested agent as a single tool call.

A sub-agent is an ordinary :class:`~agent.core.agent.Agent` built from a config
*derived* from its parent's, run to completion by
:meth:`~agent.core.agent.Agent.chat`, whose final message becomes the tool
result.  The calling model supplies only a **profile name** and a **task**:
everything that decides what the child may do — tools, provider, sandbox,
allowed paths — comes from ``config.subagents.agents`` and from the parent's own
config, never from the model.

The invariants that keep a sub-agent from being a privilege-escalation path:

* ``safety`` is deep-copied from the parent, so the child inherits the same
  sandbox mode, denied paths and approval requirements.
* The child's built-in tools are **intersected** with the parent's, so a child
  is never more capable than the agent that spawned it.
* The parent's approval callback is passed down, so writes and commands still
  reach the same human.
* ``max_depth`` decrements on every level, and at zero the tool is simply not
  registered — a leaf agent has no ``spawn_agent`` to call.

Off by default (``subagents.enabled``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from agent.core.config import AgentConfig, SubAgentProfile
from agent.tools.registry import ToolRegistry
from agent.tools.schema import SideEffect, ToolSpec

if TYPE_CHECKING:  # pragma: no cover - import cycle: agent.py imports this module
    from agent.core.agent import Agent

logger = logging.getLogger(__name__)

TOOL_NAME = "spawn_agent"


def build_child_config(
    parent: AgentConfig,
    profile: SubAgentProfile,
    depth_remaining: int,
) -> AgentConfig:
    """Derive a sub-agent's config from its parent's.

    Everything not named here is inherited verbatim — deliberately, since that
    is what carries the sandbox, the denied paths and the session directory.
    """
    child = parent.model_copy(deep=True)

    # The child may only use built-ins the parent itself holds.
    allowed = set(parent.tools.enabled_builtins)
    child.tools.enabled_builtins = [t for t in profile.tools if t in allowed]

    child.max_steps = profile.max_steps
    child.timeout = float(profile.timeout)

    # ``system_prompt`` is rebuilt by Agent.__init__; the override is the field
    # that actually survives.
    child.system_prompt = ""
    child.system_prompt_override = profile.system_prompt

    child.subagents.max_depth = depth_remaining

    if profile.provider:
        child.provider = parent.resolve_provider(profile.provider)

    return child


def _describe(agents: dict[str, SubAgentProfile]) -> str:
    lines = []
    for name, profile in sorted(agents.items()):
        desc = profile.description or "(no description)"
        tools = ", ".join(profile.tools) or "no built-in tools"
        lines.append(f"- {name}: {desc} [{tools}]")
    return "\n".join(lines)


def register_subagent_tool(registry: ToolRegistry, *, parent: Agent) -> bool:
    """Register ``spawn_agent`` into *registry*. Returns True if it was added.

    Nothing is registered when sub-agents are disabled, no profiles are
    declared, or the depth budget is exhausted — the model should not see a
    tool that can only ever return an error.
    """
    cfg = parent.config.subagents
    if not cfg.enabled or not cfg.agents or cfg.max_depth <= 0:
        return False

    agents = cfg.agents
    depth_remaining = cfg.max_depth - 1

    async def spawn_agent(agent_name: str, task: str) -> str:
        profile = agents.get(agent_name)
        if profile is None:
            available = ", ".join(sorted(agents))
            return f"Error: unknown agent {agent_name!r}. Available: {available}"

        # Deferred: agent.py imports this module at registration time.
        from agent.core.agent import Agent as _Agent
        from agent.memory.session_store import SessionStore

        child_config = build_child_config(parent.config, profile, depth_remaining)
        child = _Agent(child_config, approval_callback=parent.approval_callback)

        _emit(
            parent,
            agent_name=agent_name,
            task=task,
            status="started",
            depth_remaining=depth_remaining,
        )

        t_start = time.monotonic()
        try:
            output = await asyncio.wait_for(child.chat(task), timeout=profile.timeout or None)
        except asyncio.TimeoutError:
            _emit(
                parent,
                agent_name=agent_name,
                task=task,
                status="timeout",
                depth_remaining=depth_remaining,
                duration_ms=(time.monotonic() - t_start) * 1000,
                error=f"timed out after {profile.timeout}s",
            )
            return f"Error: sub-agent {agent_name!r} timed out after {profile.timeout}s"
        except Exception as exc:
            logger.debug("Sub-agent %r failed", agent_name, exc_info=True)
            _emit(
                parent,
                agent_name=agent_name,
                task=task,
                status="failed",
                depth_remaining=depth_remaining,
                duration_ms=(time.monotonic() - t_start) * 1000,
                error=f"{type(exc).__name__}: {exc}",
            )
            return f"Error: sub-agent {agent_name!r} failed: {type(exc).__name__}: {exc}"

        elapsed = (time.monotonic() - t_start) * 1000
        session = child.last_session
        session_id = session.session_id if session is not None else ""
        steps = len(session.events) if session is not None else 0

        # Persist the child's transcript so ``aar sessions`` can open it — the
        # parent only ever sees the final message.
        if session is not None:
            try:
                SessionStore(child_config.session_dir).save(session)
            except Exception:  # pragma: no cover - persistence is best-effort
                logger.warning("Could not persist sub-agent session", exc_info=True)

        _emit(
            parent,
            agent_name=agent_name,
            task=task,
            status="completed",
            depth_remaining=depth_remaining,
            child_session_id=session_id,
            steps=steps,
            duration_ms=elapsed,
        )

        footer = f"\n\n[sub-agent {agent_name}: {elapsed / 1000:.1f}s"
        if session_id:
            footer += f", session {session_id}"
        footer += "]"
        return (output or "(the sub-agent produced no output)") + footer

    # The longest profile decides the tool's outer guard; each run is bounded
    # individually by its own profile timeout above.
    longest = max((p.timeout for p in agents.values()), default=600)

    registry.add(
        ToolSpec(
            name=TOOL_NAME,
            description=(
                "Delegate a self-contained task to a sub-agent and return its final answer. "
                "Each agent is preconfigured with its own tools and instructions; you choose "
                "which one to use and what to ask it, nothing else. The sub-agent starts with "
                "no memory of this conversation, so the task must be self-contained. "
                "Available agents:\n" + _describe(agents)
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_name": {
                        "type": "string",
                        "enum": sorted(agents),
                        "description": "Which preconfigured agent to run.",
                    },
                    "task": {
                        "type": "string",
                        "description": (
                            "The complete task for the sub-agent, including every detail it "
                            "needs — it cannot see this conversation."
                        ),
                    },
                },
                "required": ["agent_name", "task"],
            },
            side_effects=[SideEffect.EXECUTE],
            timeout_s=longest,
            prompt_snippet=(
                f"{TOOL_NAME}: delegate a self-contained task to a preconfigured sub-agent "
                f"({', '.join(sorted(agents))})"
            ),
            prompt_guidelines=[
                "Use spawn_agent for work that is self-contained and would otherwise fill this "
                "conversation with intermediate detail. The sub-agent cannot see this "
                "conversation, so restate everything it needs; you get back only its final "
                "message.",
            ],
            handler=spawn_agent,
        )
    )
    return True


def _emit(parent: Agent, **fields: object) -> None:
    """Record a :class:`SubAgentEvent` on the parent's live session."""
    from agent.core.events import SubAgentEvent

    parent.emit(SubAgentEvent(**fields))  # type: ignore[arg-type]
