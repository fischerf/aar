"""ACP-backed ``acp_terminal`` tool.

When Aar runs inside an ACP editor (Zed, etc.) the client exposes a
``terminal/*`` method family for running commands in the user's shell —
letting the agent reuse the editor's PTY, prompt styling, and output
pane. This module wraps those methods as a single aar :class:`ToolSpec`
so the LLM can run a command and get its captured output back in one
round-trip.

``register_acp_terminal_tool()`` is called from the ACP stdio transport
at session-setup time with the current client connection and session id.
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from typing import Any

from agent.tools.builtin import powershell as _ps
from agent.tools.registry import ToolRegistry
from agent.tools.schema import SideEffect, ToolSpec


async def _run_via_client(
    conn: Any,
    session_id: str,
    command: str,
    args: list[str] | None,
    cwd: str | None,
    env: dict[str, str] | None,
    timeout: float,
) -> str:
    """Drive the full ``create → wait → output → release`` terminal lifecycle.

    The handler always releases the terminal — even on timeout or error —
    so the client side does not leak PTY resources.
    """
    from acp.schema import EnvVariable

    env_list = [EnvVariable(name=k, value=v) for k, v in env.items()] if env else None

    create_resp = await conn.create_terminal(
        session_id=session_id,
        command=command,
        args=args,
        cwd=cwd,
        env=env_list,
    )
    terminal_id = create_resp.terminal_id

    exit_code: int | None = None
    signal: str | None = None
    timed_out = False

    try:
        try:
            exit_resp = await asyncio.wait_for(
                conn.wait_for_terminal_exit(session_id=session_id, terminal_id=terminal_id),
                timeout=timeout if timeout > 0 else None,
            )
            exit_code = exit_resp.exit_code
            signal = exit_resp.signal
        except asyncio.TimeoutError:
            timed_out = True
            # Best-effort: try to stop the still-running command.
            try:
                await conn.kill_terminal(session_id=session_id, terminal_id=terminal_id)
            except Exception:
                pass

        out_resp = await conn.terminal_output(session_id=session_id, terminal_id=terminal_id)
        output = out_resp.output or ""
        truncated = bool(out_resp.truncated)
    finally:
        try:
            await conn.release_terminal(session_id=session_id, terminal_id=terminal_id)
        except Exception:
            # Release failures must not mask the command's output.
            pass

    parts: list[str] = [output]
    if truncated:
        parts.append("\n[output truncated]")
    if timed_out:
        parts.append(f"\n[error: terminal command timed out after {timeout:.0f}s]")
    elif exit_code is not None and exit_code != 0:
        parts.append(f"\n[exit code: {exit_code}]")
    elif signal:
        parts.append(f"\n[terminated by signal: {signal}]")
    return "".join(parts)


_HOST_ENV = (
    "acp_terminal runs commands in the user's native host environment (with the"
    " user's PATH, Python, pip, project CLIs)"
)


def _shell_reason(name: str) -> str:
    if name == "bash":
        if _ps.is_windows_host():
            return (
                "bash routes through WSL (or Git Bash), which has a different filesystem,"
                " PATH, and installed packages"
            )
        return "bash runs in aar's own sandboxed subprocess with a filtered environment"
    return (
        "powershell runs in aar's own subprocess with a filtered environment (no"
        " PowerShell profile, no activated virtualenv)"
    )


def _shell_use_only(name: str) -> str:
    if name == "bash":
        if _ps.is_windows_host():
            return "Use bash only when you explicitly need a Linux/WSL environment."
        return "Use bash only when acp_terminal is unavailable or fails."
    return "Use powershell only when you need PowerShell itself (cmdlets, Windows administration)."


def acp_terminal_guidelines(shell_tools: Collection[str] | None = None) -> list[str]:
    """Prompt guidelines for ``acp_terminal``, naming the shell tools it competes with.

    *shell_tools* are the registered local shell tools (``bash``,
    ``powershell``, both or neither); ``None`` means not known yet and gives
    wording that holds for either.
    """
    use_for = (
        "Use acp_terminal for: python/pip, project CLIs (pytest, ruff, npm, etc.), and any"
        " command that needs the project's virtualenv or installed tools."
    )
    editor = "When running inside an editor (Zed, VS Code), "
    if shell_tools is None:
        return [
            f"{editor}prefer acp_terminal over the local shell tool (bash or powershell,"
            f" whichever is available). {_HOST_ENV}, while the local shell tool runs in"
            " aar's own subprocess with a different environment (on Windows, bash routes"
            " through WSL).",
            use_for,
        ]
    names = [n for n in ("bash", "powershell") if n in shell_tools]
    if not names:
        return [f"{editor}use acp_terminal to run commands: {_HOST_ENV}.", use_for]
    return [
        f"{editor}prefer acp_terminal over {' and '.join(names)}. {_HOST_ENV}, while "
        + "; ".join(_shell_reason(n) for n in names)
        + ".",
        " ".join([use_for, *(_shell_use_only(n) for n in names)]),
    ]


def adapt_acp_terminal_tool(registry: ToolRegistry, shell_tools: Collection[str]) -> None:
    """Re-word a registered ``acp_terminal`` for the shell tools actually registered.

    The ACP transport registers ``acp_terminal`` before the agent registers
    ``bash`` / ``powershell``, so the agent calls this once it knows which.
    """
    spec = registry.get("acp_terminal")
    if spec is not None:
        spec.prompt_guidelines = acp_terminal_guidelines(shell_tools)


def register_acp_terminal_tool(
    registry: ToolRegistry,
    conn: Any,
    session_id: str,
    default_timeout: float = 60.0,
    shell_tools: Collection[str] | None = None,
) -> None:
    """Register the ``acp_terminal`` tool in *registry*.

    The tool runs a shell command through the ACP client's terminal
    service, waits for it to exit (bounded by *default_timeout*), and
    returns the captured output.  *shell_tools* words its guidelines (see
    :func:`acp_terminal_guidelines`).
    """

    async def acp_terminal(
        command: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> str:
        """Run *command* via the ACP client's terminal and return its output."""
        return await _run_via_client(
            conn=conn,
            session_id=session_id,
            command=command,
            args=args,
            cwd=cwd,
            env=env,
            timeout=timeout if timeout is not None else default_timeout,
        )

    registry.add(
        ToolSpec(
            name="acp_terminal",
            description=(
                "Run a shell command through the connected ACP client's terminal "
                "(instead of a local subprocess). Returns captured stdout/stderr."
            ),
            prompt_snippet=(
                "Run a command in the editor's terminal (native host environment, user's PATH)"
            ),
            prompt_guidelines=acp_terminal_guidelines(shell_tools),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The command to execute.",
                    },
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Additional arguments to pass to the command.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory for the command.",
                    },
                    "env": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": "Extra environment variables as a name/value mapping.",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Max seconds to wait for exit (default 60).",
                    },
                },
                "required": ["command"],
            },
            side_effects=[SideEffect.EXECUTE],
            requires_approval=False,
            handler=acp_terminal,
        )
    )
