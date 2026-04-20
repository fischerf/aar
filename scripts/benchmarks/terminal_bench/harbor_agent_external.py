"""Aar — Harbor Framework external agent for Terminal-Bench 2.0.

The agent runs on the *host* and calls the LLM API directly.  Its bash tool
is bridged to Harbor's ``environment.exec()``, so every shell command executes
inside the task container.

Limitation: the built-in ``read_file`` / ``write_file`` / ``edit_file`` tools
operate on the *host* filesystem.  This implementation replaces them with
container-proxied equivalents so all file I/O also hits the container.

Usage:
    harbor run -d "terminal-bench@2.0" \
        --agent-import-path scripts.benchmarks.terminal_bench.harbor_agent_external:AarExternalAgent \
        --model anthropic/claude-sonnet-4-6
"""

from __future__ import annotations

import base64
import shlex

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

import agent as aar
from agent.core.config import AgentConfig, ProviderConfig, SafetyConfig, ToolConfig
from agent.tools.registry import ToolRegistry
from agent.tools.schema import SideEffect, ToolSpec


def _build_registry(environment: BaseEnvironment) -> ToolRegistry:
    """Return a ToolRegistry whose tools all delegate to the container."""
    registry = ToolRegistry()

    # ------------------------------------------------------------------ bash
    async def bash(command: str, timeout: int = 120) -> str:
        r = await environment.exec(command, timeout_sec=timeout)
        parts: list[str] = []
        if r.stdout:
            parts.append(r.stdout)
        if r.stderr:
            parts.append(f"STDERR:\n{r.stderr}")
        if r.return_code != 0:
            parts.append(f"Exit code: {r.return_code}")
        return "\n".join(parts) or "(no output)"

    registry.add(ToolSpec(
        name="bash",
        description=(
            "Execute a shell command inside the task container. "
            "Returns stdout, stderr, and exit code. "
            "Pass a larger timeout for slow commands."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 120)"},
            },
            "required": ["command"],
        },
        side_effects=[SideEffect.EXECUTE],
        handler=bash,
    ))

    # -------------------------------------------------------------- read_file
    async def read_file(path: str) -> str:
        r = await environment.exec(f"cat {shlex.quote(path)}", timeout_sec=30)
        if r.return_code != 0:
            return f"Error reading {path}: {r.stderr}"
        return r.stdout or ""

    registry.add(ToolSpec(
        name="read_file",
        description="Read a file from the task container.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute or relative path"}},
            "required": ["path"],
        },
        side_effects=[SideEffect.READ],
        handler=read_file,
    ))

    # ------------------------------------------------------------- write_file
    async def write_file(path: str, content: str) -> str:
        # Base64-encode content to avoid quoting issues with arbitrary text
        b64 = base64.b64encode(content.encode()).decode()
        cmd = f"mkdir -p $(dirname {shlex.quote(path)}) && printf %s {shlex.quote(b64)} | base64 -d > {shlex.quote(path)}"
        r = await environment.exec(cmd, timeout_sec=30)
        if r.return_code != 0:
            return f"Error writing {path}: {r.stderr}"
        return f"Wrote {path}"

    registry.add(ToolSpec(
        name="write_file",
        description="Write content to a file in the task container.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string", "description": "File content to write"},
            },
            "required": ["path", "content"],
        },
        side_effects=[SideEffect.WRITE],
        handler=write_file,
    ))

    # --------------------------------------------------------------- edit_file
    async def edit_file(path: str, old_string: str, new_string: str) -> str:
        # Read → replace → write
        r = await environment.exec(f"cat {shlex.quote(path)}", timeout_sec=30)
        if r.return_code != 0:
            return f"Error reading {path}: {r.stderr}"
        original = r.stdout or ""
        if old_string not in original:
            return f"Error: old_string not found in {path}"
        updated = original.replace(old_string, new_string, 1)
        b64 = base64.b64encode(updated.encode()).decode()
        cmd = f"printf %s {shlex.quote(b64)} | base64 -d > {shlex.quote(path)}"
        r2 = await environment.exec(cmd, timeout_sec=30)
        if r2.return_code != 0:
            return f"Error writing {path}: {r2.stderr}"
        return f"Edited {path}"

    registry.add(ToolSpec(
        name="edit_file",
        description="Replace the first occurrence of old_string with new_string in a container file.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string", "description": "Exact text to replace"},
                "new_string": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        side_effects=[SideEffect.WRITE],
        handler=edit_file,
    ))

    # --------------------------------------------------------- list_directory
    async def list_directory(path: str = ".") -> str:
        r = await environment.exec(f"ls -la {shlex.quote(path)}", timeout_sec=30)
        if r.return_code != 0:
            return f"Error listing {path}: {r.stderr}"
        return r.stdout or "(empty)"

    registry.add(ToolSpec(
        name="list_directory",
        description="List files in a directory inside the task container.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Directory path (default: .)"}},
        },
        side_effects=[SideEffect.READ],
        handler=list_directory,
    ))

    return registry


class AarExternalAgent(BaseAgent):
    """Aar agent running on the host; all tool I/O routed into the container."""

    @staticmethod
    def name() -> str:
        return "aar-external"

    def version(self) -> str | None:
        return aar.__version__

    async def setup(self, environment: BaseEnvironment) -> None:
        pass  # nothing to provision on the host

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        if self.model_name and "/" in self.model_name:
            provider_name, model_id = self.model_name.split("/", 1)
        else:
            provider_name = "anthropic"
            model_id = self.model_name or "claude-sonnet-4-6"

        config = AgentConfig(
            provider=ProviderConfig(name=provider_name, model=model_id),
            tools=ToolConfig(enabled_builtins=[]),  # we supply all tools manually
            # Disable interactive approval gates.  With approval_callback=None (the
            # default when constructing Agent directly), require_approval=True causes
            # the policy to return DENY for every write/execute tool call.
            safety=SafetyConfig(
                require_approval_for_writes=False,
                require_approval_for_execute=False,
            ),
            max_steps=50,
        )

        registry = _build_registry(environment)
        agent = aar.Agent(config=config, registry=registry)
        session = await agent.run(instruction)

        context.n_input_tokens = session.total_input_tokens
        context.n_output_tokens = session.total_output_tokens
        context.cost_usd = session.total_cost
