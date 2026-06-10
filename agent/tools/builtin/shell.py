"""Built-in shell/subprocess tool."""

from __future__ import annotations

import asyncio
import os

from agent.safety.sandbox import Sandbox
from agent.tools.registry import ToolRegistry
from agent.tools.schema import SideEffect, ToolSpec


def register_shell_tools(
    registry: ToolRegistry,
    sandbox: Sandbox | None = None,
    default_timeout: int = 120,
) -> None:
    """Register the bash tool into the given registry.

    When *sandbox* is provided, all commands are executed through it (applying
    whatever isolation the sandbox implements).  Falls back to direct subprocess
    creation when *sandbox* is None, preserving backwards compatibility.

    *default_timeout* is the timeout (seconds) used when the model omits the
    ``timeout`` argument.  Pass ``config.tools.bash_default_timeout`` here so
    the config drives the behaviour instead of a hardcoded value.
    """

    async def bash(command: str, timeout: int = default_timeout) -> str:
        """Execute a shell command and return stdout + stderr."""
        if sandbox is not None:
            result = await sandbox.execute(command, timeout=timeout)
            return result.output

        # Fallback: direct subprocess (sandbox=None).
        # On Windows, bash resolves to WSL; on Unix use the system shell.
        if os.name == "nt":
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=os.getcwd(),
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=os.getcwd(),
            )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return f"Error: command timed out after {timeout}s"

        output_parts = []
        if stdout:
            output_parts.append(stdout.decode("utf-8", errors="replace"))
        if stderr:
            output_parts.append(f"STDERR:\n{stderr.decode('utf-8', errors='replace')}")
        if proc.returncode != 0:
            output_parts.append(f"Exit code: {proc.returncode}")
        return "\n".join(output_parts) if output_parts else "(no output)"

    registry.add(
        ToolSpec(
            name="bash",
            description=(
                "Execute a shell command. Returns stdout, stderr, and exit code. "
                f"Default timeout: {default_timeout}s — increase for slow commands."
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
                        "description": f"Timeout in seconds (default: {default_timeout}). Increase for slow commands.",
                    },
                },
                "required": ["command"],
            },
            side_effects=[SideEffect.EXECUTE],
            handler=bash,
        )
    )
