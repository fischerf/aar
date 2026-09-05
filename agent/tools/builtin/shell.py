"""Built-in shell/subprocess tool."""

from __future__ import annotations

from agent.safety.sandbox import LocalSandbox, Sandbox
from agent.tools.registry import ToolRegistry
from agent.tools.schema import SideEffect, ToolSpec

# M1 — Absolute upper bound for the model-supplied ``timeout`` when the caller
# gives no executor-derived cap. Without a bound the model can request
# ``timeout=10**9`` and, if the executor's outer guard is disabled
# (``tools.command_timeout = 0``), hang the run forever.
DEFAULT_TIMEOUT_HARD_CAP = 3600


def register_shell_tools(
    registry: ToolRegistry,
    sandbox: Sandbox | None = None,
    default_timeout: int = 120,
    hard_cap: int | None = None,
) -> None:
    """Register the bash tool into the given registry.

    All commands are executed through *sandbox*.  When *sandbox* is None a
    plain :class:`LocalSandbox` is created, so there is exactly one process
    management code path (C2's timeout/process-group handling lands in one
    place instead of being duplicated here).

    *default_timeout* is the timeout (seconds) used when the model omits the
    ``timeout`` argument.  Pass ``config.tools.bash_default_timeout`` here so
    the config drives the behaviour instead of a hardcoded value.

    *hard_cap* clamps whatever the model asks for.  Pass
    ``config.tools.command_timeout`` so the tool never outlives the executor's
    outer guard; ``None`` / ``0`` falls back to ``DEFAULT_TIMEOUT_HARD_CAP``.
    """
    active_sandbox = sandbox if sandbox is not None else LocalSandbox()
    cap = hard_cap or DEFAULT_TIMEOUT_HARD_CAP
    effective_default = max(1, min(default_timeout, cap))

    async def bash(command: str, timeout: int = effective_default) -> str:
        """Execute a shell command and return stdout + stderr."""
        # M1 — clamp: ``timeout`` arrives straight from the model.
        timeout = max(1, min(int(timeout), cap))
        result = await active_sandbox.execute(command, timeout=timeout)
        if result.timed_out:
            return f"Error: command timed out after {timeout}s"
        return result.output

    registry.add(
        ToolSpec(
            name="bash",
            description=(
                "Execute a shell command. Returns stdout, stderr, and exit code. "
                f"Default timeout: {effective_default}s — increase for slow commands "
                f"(maximum {cap}s)."
            ),
            prompt_snippet=("Execute a shell command (returns stdout, stderr, exit code)"),
            prompt_guidelines=[
                "On Windows, bash executes inside WSL (Linux subsystem). The Windows"
                " project directory D:\\path is accessible at /mnt/d/path, but Python,"
                " pip, and project CLIs installed on Windows may not be available in"
                " WSL. If a command fails with 'command not found' or import errors,"
                " switch to acp_terminal (if available) which uses the native Windows"
                " host environment.",
            ],
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to execute"},
                    "timeout": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": cap,
                        "description": (
                            f"Timeout in seconds (default: {effective_default}, max: {cap}). "
                            "Increase for slow commands."
                        ),
                    },
                },
                "required": ["command"],
            },
            side_effects=[SideEffect.EXECUTE],
            handler=bash,
        )
    )
