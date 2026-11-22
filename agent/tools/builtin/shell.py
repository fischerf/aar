"""Built-in shell/subprocess tool."""

from __future__ import annotations

import logging
import shutil
from typing import TYPE_CHECKING

from agent.safety.sandbox import (
    PROGRAM_EXEC_MODES,
    LocalSandbox,
    Sandbox,
    resolve_sandbox_mode,
)
from agent.tools.builtin import powershell as _ps
from agent.tools.registry import ToolRegistry
from agent.tools.schema import SideEffect, ToolSpec

if TYPE_CHECKING:
    from agent.core.config import ToolConfig

logger = logging.getLogger(__name__)

# Built-in names that ``register_configured_shell_tools`` owns.
SHELL_TOOL_NAMES = frozenset({"bash", "powershell"})

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


def bash_available(sandbox_mode: str) -> bool:
    """Whether the ``bash`` tool can run under *sandbox_mode* on this host.

    Off Windows there is always a shell; the ``wsl`` mode brings its own.
    Elsewhere on Windows ``bash -c`` needs WSL's ``bash.exe`` or Git Bash on
    ``PATH`` — an office PC typically has neither.
    """
    if not _ps.is_windows_host() or resolve_sandbox_mode(sandbox_mode) == "wsl":
        return True
    return shutil.which("bash") is not None


def register_configured_shell_tools(
    registry: ToolRegistry,
    tools_config: ToolConfig,
    sandbox_mode: str,
    sandbox: Sandbox | None = None,
) -> set[str]:
    """Register ``bash`` and/or ``powershell`` as configured; return the names added.

    - ``bash`` is registered exactly as before whenever it is enabled and can
      run here.
    - ``powershell`` is added when it is in ``enabled_builtins``, an executable
      resolves (``tools.powershell.executable``) and the sandbox runs host
      programs (``local``, ``windows`` — not ``wsl`` / ``linux``).
    - With ``tools.powershell.fallback_for_bash`` (default), a Windows host
      whose config enables ``bash`` but has no bash gets ``powershell`` in its
      place. If ``powershell`` cannot be registered either, ``bash`` is
      registered anyway (the historical behaviour).
    - Under ``wsl`` / ``linux`` a config that enables only ``powershell`` gets
      ``bash`` — the sandbox's own shell — instead of no shell at all.

    *sandbox* is the live backend when there is one (the agent); prompt and
    ``/tools`` listings pass ``None`` and the decision follows *sandbox_mode*.
    The returned names may differ from ``enabled_builtins`` — callers that
    prune to ``enabled_builtins`` must keep them.
    """
    enabled = set(tools_config.enabled_builtins)
    want_bash = "bash" in enabled
    bash_ok = want_bash and bash_available(sandbox_mode)
    ps_cfg = tools_config.powershell
    want_ps = "powershell" in enabled or (want_bash and not bash_ok and ps_cfg.fallback_for_bash)

    if sandbox is not None:
        program_exec = sandbox.supports_program_exec
        ps_sandbox = sandbox
    else:
        program_exec = resolve_sandbox_mode(sandbox_mode) in PROGRAM_EXEC_MODES
        ps_sandbox = LocalSandbox()  # listings only need the spec, never run it

    registered: set[str] = set()
    if want_ps:
        exe = _ps.resolve_powershell_executable(ps_cfg.executable) if program_exec else None
        if not program_exec:
            logger.info(
                "powershell tool skipped: sandbox mode %r runs no host programs", sandbox_mode
            )
        elif exe is None:
            logger.info("powershell tool skipped: no executable for %r", ps_cfg.executable)
        elif _ps.register_powershell_tool(
            registry,
            sandbox=ps_sandbox,
            executable=exe,
            default_timeout=tools_config.bash_default_timeout,
            hard_cap=tools_config.command_timeout,
        ):
            registered.add("powershell")
            if want_bash and not bash_ok:
                logger.info("bash not found on this Windows host; using the powershell tool")

    stand_in = not want_bash and "powershell" in enabled and not program_exec
    if stand_in:
        logger.info("sandbox mode %r runs shell commands only; using the bash tool", sandbox_mode)
    if (want_bash and (bash_ok or "powershell" not in registered)) or stand_in:
        register_shell_tools(
            registry,
            sandbox=sandbox,
            default_timeout=tools_config.bash_default_timeout,
            hard_cap=tools_config.command_timeout,
        )
        registered.add("bash")
    return registered
