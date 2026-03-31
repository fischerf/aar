"""Sandbox execution backends — pluggable isolation for tool execution."""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class SandboxResult:
    """Result from a sandboxed command execution."""

    __slots__ = ("stdout", "stderr", "exit_code", "timed_out")

    def __init__(
        self,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
        timed_out: bool = False,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.timed_out = timed_out

    @property
    def output(self) -> str:
        parts = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(f"STDERR:\n{self.stderr}")
        if self.exit_code != 0:
            parts.append(f"Exit code: {self.exit_code}")
        if self.timed_out:
            parts.append("(timed out)")
        return "\n".join(parts) if parts else "(no output)"


async def _create_subprocess(
    command: str,
    cwd: str,
    env: dict[str, str] | None,
) -> asyncio.subprocess.Process:
    """Create a subprocess using bash on Windows, shell on Unix."""
    if os.name == "nt":
        return await asyncio.create_subprocess_exec(
            "bash", "-c", command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
    return await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )


class Sandbox(ABC):
    """Abstract sandbox backend."""

    @abstractmethod
    async def execute(
        self,
        command: str,
        timeout: int = 30,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> SandboxResult: ...

    async def close(self) -> None:
        """Clean up sandbox resources."""
        pass


class LocalSandbox(Sandbox):
    """Direct local subprocess execution (trusted dev environments)."""

    def __init__(
        self,
        default_cwd: str | None = None,
        restricted_env: bool = False,
    ) -> None:
        self.default_cwd = default_cwd or os.getcwd()
        self.restricted_env = restricted_env

    async def execute(
        self,
        command: str,
        timeout: int = 30,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        work_dir = cwd or self.default_cwd
        proc_env = self._build_env(env)

        proc = await _create_subprocess(command, work_dir, proc_env)

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return SandboxResult(timed_out=True, exit_code=-1)

        return SandboxResult(
            stdout=stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else "",
            stderr=stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else "",
            exit_code=proc.returncode or 0,
        )

    def _build_env(self, extra: dict[str, str] | None) -> dict[str, str] | None:
        if not self.restricted_env and not extra:
            return None  # inherit parent env
        base = dict(os.environ) if not self.restricted_env else {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", os.environ.get("USERPROFILE", "")),
            "TERM": os.environ.get("TERM", "xterm"),
        }
        if extra:
            base.update(extra)
        return base


class SubprocessSandbox(Sandbox):
    """Isolated subprocess with restricted capabilities.

    Uses resource limits and environment isolation without requiring containers.
    """

    def __init__(
        self,
        default_cwd: str | None = None,
        max_memory_mb: int = 512,
        allowed_env_vars: list[str] | None = None,
    ) -> None:
        self.default_cwd = default_cwd or os.getcwd()
        self.max_memory_mb = max_memory_mb
        self.allowed_env_vars = allowed_env_vars or ["PATH", "HOME", "TERM", "LANG"]

    async def execute(
        self,
        command: str,
        timeout: int = 30,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        work_dir = cwd or self.default_cwd
        proc_env = {k: os.environ[k] for k in self.allowed_env_vars if k in os.environ}
        if os.name == "nt":
            # Windows requires these for processes to load correctly
            for var in ("SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "USERPROFILE"):
                if var not in proc_env and var in os.environ:
                    proc_env[var] = os.environ[var]
        if env:
            proc_env.update(env)

        # On Unix, we can use ulimit to restrict resources
        wrapped = self._wrap_command(command)

        proc = await _create_subprocess(wrapped, work_dir, proc_env)

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return SandboxResult(timed_out=True, exit_code=-1)

        return SandboxResult(
            stdout=stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else "",
            stderr=stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else "",
            exit_code=proc.returncode or 0,
        )

    def _wrap_command(self, command: str) -> str:
        """Wrap command with resource limits where available."""
        if os.name == "nt":
            return command  # ulimit is unreliable in Git Bash on Windows
        mem_kb = self.max_memory_mb * 1024
        return f"ulimit -v {mem_kb} 2>/dev/null; {command}"
