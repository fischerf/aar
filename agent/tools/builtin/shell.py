"""Built-in shell/subprocess tool."""

from __future__ import annotations

import asyncio
import os

from agent.tools.registry import ToolRegistry
from agent.tools.schema import SideEffect, ToolSpec


def register_shell_tools(registry: ToolRegistry, shell_path: str = "") -> None:
    """Register the bash tool into the given registry."""

    async def bash(command: str, timeout: int = 30) -> str:
        """Execute a shell command and return stdout + stderr."""
        # Use configured shell, or fall back to Git Bash on Windows /
        # system shell on Unix.
        if shell_path:
            proc = await asyncio.create_subprocess_exec(
                shell_path,
                "-c",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=os.getcwd(),
            )
        elif os.name == "nt":
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
                "On Windows commands run via Git Bash (bash -c), so standard Unix/bash "
                "syntax works (ls, cat, grep, find, …). Use Windows-style paths for "
                "file tools, but bash syntax for shell commands."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to execute"},
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default: 30)",
                    },
                },
                "required": ["command"],
            },
            side_effects=[SideEffect.EXECUTE],
            handler=bash,
        )
    )
