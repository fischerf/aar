"""Sandbox execution backends — pluggable isolation for tool execution."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import sys
import tempfile
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Windows integrity-level helper script
# ---------------------------------------------------------------------------
# Spawned as a subprocess to self-lower its mandatory integrity level to
# "Low" (S-1-16-4096) before running the actual command.  The helper process
# inherits the asyncio stdout/stderr pipes so captured output flows through
# transparently.  Bash (or the configured shell) is launched as a child of
# this helper, so it also runs at Low integrity and inherits the Job Object
# assigned to the helper's PID.
_WINDOWS_INTEGRITY_HELPER = """\
import sys, ctypes, ctypes.wintypes as wt, subprocess

def _set_low_integrity():
    ADVAPI32 = ctypes.WinDLL("advapi32", use_last_error=True)
    KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class SID_IDENTIFIER_AUTHORITY(ctypes.Structure):
        _fields_ = [("Value", ctypes.c_byte * 6)]

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]

    class TOKEN_MANDATORY_LABEL(ctypes.Structure):
        _fields_ = [("Label", SID_AND_ATTRIBUTES)]

    MANDATORY_LABEL_AUTH = SID_IDENTIFIER_AUTHORITY(
        Value=(ctypes.c_byte * 6)(0, 0, 0, 0, 0, 16)
    )
    SECURITY_MANDATORY_LOW_RID = 0x1000
    SE_GROUP_INTEGRITY = 0x00000020
    TOKEN_ADJUST_DEFAULT = 0x0080
    TokenIntegrityLevel = 25

    sid = ctypes.c_void_p()
    if not ADVAPI32.AllocateAndInitializeSid(
        ctypes.byref(MANDATORY_LABEL_AUTH), 1, SECURITY_MANDATORY_LOW_RID,
        0, 0, 0, 0, 0, 0, 0, ctypes.byref(sid)
    ):
        return
    try:
        label = TOKEN_MANDATORY_LABEL()
        label.Label.Sid = sid.value
        label.Label.Attributes = SE_GROUP_INTEGRITY
        token = wt.HANDLE()
        if ADVAPI32.OpenProcessToken(
            KERNEL32.GetCurrentProcess(), TOKEN_ADJUST_DEFAULT, ctypes.byref(token)
        ):
            try:
                ADVAPI32.GetLengthSid.restype = wt.DWORD
                size = ctypes.sizeof(TOKEN_MANDATORY_LABEL) + ADVAPI32.GetLengthSid(sid)
                ADVAPI32.SetTokenInformation(token, TokenIntegrityLevel, ctypes.byref(label), size)
            finally:
                KERNEL32.CloseHandle(token)
    finally:
        ADVAPI32.FreeSid(sid)

_set_low_integrity()
_shell = sys.argv[1] if len(sys.argv) > 1 else "bash"
_cmd = sys.argv[2] if len(sys.argv) > 2 else ""
result = subprocess.run([_shell, "-c", _cmd])
sys.exit(result.returncode)
"""


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
    shell_path: str = "",
    **kwargs: object,
) -> asyncio.subprocess.Process:
    """Create a subprocess using the configured shell, bash on Windows, or system shell on Unix."""
    if shell_path:
        return await asyncio.create_subprocess_exec(
            shell_path,
            "-c",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            **kwargs,
        )
    if os.name == "nt":
        return await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            **kwargs,
        )
    return await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        **kwargs,
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
        shell_path: str = "",
    ) -> None:
        self.default_cwd = default_cwd or os.getcwd()
        self.restricted_env = restricted_env
        self.shell_path = shell_path

    async def execute(
        self,
        command: str,
        timeout: int = 30,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        work_dir = cwd or self.default_cwd
        proc_env = self._build_env(env)

        proc = await _create_subprocess(command, work_dir, proc_env, shell_path=self.shell_path)

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
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
        base = (
            dict(os.environ)
            if not self.restricted_env
            else {
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", os.environ.get("USERPROFILE", "")),
                "TERM": os.environ.get("TERM", "xterm"),
            }
        )
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
        shell_path: str = "",
    ) -> None:
        self.default_cwd = default_cwd or os.getcwd()
        self.max_memory_mb = max_memory_mb
        self.allowed_env_vars = allowed_env_vars or ["PATH", "HOME", "TERM", "LANG"]
        self.shell_path = shell_path

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

        proc = await _create_subprocess(wrapped, work_dir, proc_env, shell_path=self.shell_path)

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
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


class WorkspaceSandbox(Sandbox):
    """Linux sandbox: Landlock LSM restricts subprocess to workspace + ulimit memory cap.

    The subprocess can read and execute anywhere on the filesystem but can only
    write within *workspace*.  Requires Linux kernel >= 5.13 for Landlock.
    Falls back to environment restriction + ulimit when Landlock is unavailable,
    logging a warning.
    """

    def __init__(
        self,
        workspace: str | None = None,
        max_memory_mb: int = 512,
        allowed_env_vars: list[str] | None = None,
        shell_path: str = "",
    ) -> None:
        self.workspace = workspace or os.getcwd()
        self.max_memory_mb = max_memory_mb
        self.allowed_env_vars = allowed_env_vars or ["PATH", "HOME", "TERM", "LANG"]
        self.shell_path = shell_path
        self._landlock_available: bool | None = None

    # ------------------------------------------------------------------
    # Landlock availability probe (cached per instance)
    # ------------------------------------------------------------------

    def _check_landlock(self) -> bool:
        if self._landlock_available is not None:
            return self._landlock_available
        try:
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            libc.syscall.restype = ctypes.c_long
            # syscall 444 (landlock_create_ruleset) with flags=1 queries ABI version.
            # Returns the ABI version (>= 1) when available, -1 otherwise.
            abi = libc.syscall(444, 0, 0, 1)
            self._landlock_available = bool(abi > 0)
        except Exception:
            self._landlock_available = False
        logger.debug("Landlock available: %s", self._landlock_available)
        return self._landlock_available

    # ------------------------------------------------------------------
    # preexec_fn factory — runs inside the forked child before exec
    # ------------------------------------------------------------------

    def _make_landlock_preexec(self, workspace: str):  # type: ignore[return]
        """Return a zero-argument callable that applies Landlock in the child process."""

        def _apply() -> None:
            # All code here runs in the forked child.  No asyncio, no logging.
            try:
                import ctypes
                import os as _os

                libc = ctypes.CDLL(None, use_errno=True)
                libc.syscall.restype = ctypes.c_long

                # Landlock syscall numbers (stable across archs since 5.13)
                NR_CREATE = 444
                NR_ADD = 445
                NR_RESTRICT = 446
                LANDLOCK_RULE_PATH_BENEATH = 1
                PR_SET_NO_NEW_PRIVS = 38

                # Landlock ABI v1 access-right flags (kernel 5.13+)
                FS_EXECUTE = 1 << 0
                FS_WRITE_FILE = 1 << 1
                FS_READ_FILE = 1 << 2
                FS_READ_DIR = 1 << 3
                FS_REMOVE_DIR = 1 << 4
                FS_REMOVE_FILE = 1 << 5
                FS_MAKE_CHAR = 1 << 6
                FS_MAKE_DIR = 1 << 7
                FS_MAKE_REG = 1 << 8
                FS_MAKE_SOCK = 1 << 9
                FS_MAKE_FIFO = 1 << 10
                FS_MAKE_BLOCK = 1 << 11
                FS_MAKE_SYM = 1 << 12

                ALL_V1 = (
                    FS_EXECUTE
                    | FS_WRITE_FILE
                    | FS_READ_FILE
                    | FS_READ_DIR
                    | FS_REMOVE_DIR
                    | FS_REMOVE_FILE
                    | FS_MAKE_CHAR
                    | FS_MAKE_DIR
                    | FS_MAKE_REG
                    | FS_MAKE_SOCK
                    | FS_MAKE_FIFO
                    | FS_MAKE_BLOCK
                    | FS_MAKE_SYM
                )
                # Root rule: read + execute everywhere (no writes outside workspace)
                READ_EXEC = FS_EXECUTE | FS_READ_FILE | FS_READ_DIR

                class RulesetAttr(ctypes.Structure):
                    _fields_ = [("handled_access_fs", ctypes.c_uint64)]

                class PathBeneathAttr(ctypes.Structure):
                    _pack_ = 1
                    _fields_ = [
                        ("allowed_access", ctypes.c_uint64),
                        ("parent_fd", ctypes.c_int32),
                    ]

                # Create ruleset covering all v1 access types
                attr = RulesetAttr(handled_access_fs=ALL_V1)
                ruleset_fd = libc.syscall(NR_CREATE, ctypes.byref(attr), ctypes.sizeof(attr), 0)
                if ruleset_fd < 0:
                    return  # Landlock not available — silent fallback

                O_PATH = getattr(_os, "O_PATH", 0x200000)

                # Rule 1: read + execute on filesystem root (everywhere, no writes)
                root_fd = _os.open("/", O_PATH | _os.O_DIRECTORY)
                try:
                    ra = PathBeneathAttr(allowed_access=READ_EXEC, parent_fd=root_fd)
                    libc.syscall(
                        NR_ADD, ruleset_fd, LANDLOCK_RULE_PATH_BENEATH, ctypes.byref(ra), 0
                    )
                finally:
                    _os.close(root_fd)

                # Rule 2: full access within workspace
                try:
                    ws_fd = _os.open(workspace, O_PATH | _os.O_DIRECTORY)
                    try:
                        wa = PathBeneathAttr(allowed_access=ALL_V1, parent_fd=ws_fd)
                        libc.syscall(
                            NR_ADD, ruleset_fd, LANDLOCK_RULE_PATH_BENEATH, ctypes.byref(wa), 0
                        )
                    finally:
                        _os.close(ws_fd)
                except OSError:
                    pass  # workspace may not exist yet; skip write rule

                # PR_SET_NO_NEW_PRIVS is required before landlock_restrict_self
                libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)

                # Apply restriction — from this point the child is restricted
                libc.syscall(NR_RESTRICT, ruleset_fd, 0)
                _os.close(ruleset_fd)

            except Exception:
                pass  # Silent fallback — restriction not applied

        return _apply

    # ------------------------------------------------------------------
    # execute
    # ------------------------------------------------------------------

    async def execute(
        self,
        command: str,
        timeout: int = 30,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        work_dir = cwd or self.workspace
        proc_env = {k: os.environ[k] for k in self.allowed_env_vars if k in os.environ}
        if env:
            proc_env.update(env)

        if sys.platform.startswith("linux"):
            wrapped = f"ulimit -v {self.max_memory_mb * 1024} 2>/dev/null; {command}"
        else:
            wrapped = command

        kwargs: dict[str, object] = {}
        if sys.platform.startswith("linux"):
            if self._check_landlock():
                kwargs["preexec_fn"] = self._make_landlock_preexec(self.workspace)
            else:
                logger.warning(
                    "WorkspaceSandbox: Landlock unavailable (kernel < 5.13 or LSM disabled); "
                    "falling back to env restriction + ulimit only"
                )

        proc = await _create_subprocess(
            wrapped, work_dir, proc_env, shell_path=self.shell_path, **kwargs
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return SandboxResult(timed_out=True, exit_code=-1)

        return SandboxResult(
            stdout=stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else "",
            stderr=stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else "",
            exit_code=proc.returncode or 0,
        )


class WindowsSubprocessSandbox(Sandbox):
    """Windows sandbox: Job Object resource limits + optional Low Integrity Level.

    Layers of protection:
    1. Restricted environment variables (only essential vars passed to subprocess).
    2. Windows Job Object (via ctypes kernel32) — enforces memory working-set limit,
       active process count, and ``KILL_ON_JOB_CLOSE`` so orphaned processes are
       cleaned up automatically.
    3. Low Integrity Level (optional, default enabled) — the helper process
       self-lowers its mandatory integrity to *Low* (S-1-16-4096) before spawning
       the shell.  A Low-integrity process cannot write to Medium/High-integrity
       locations (user profile, Program Files, registry), effectively restricting
       writes to the workspace (which is stamped as Low-integrity-writable via
       ``icacls``).
    """

    _helper_path: str | None = None  # shared across instances, written once

    def __init__(
        self,
        workspace: str | None = None,
        max_memory_mb: int = 512,
        max_processes: int = 10,
        allowed_env_vars: list[str] | None = None,
        use_low_integrity: bool = True,
        shell_path: str = "",
    ) -> None:
        self.workspace = workspace or os.getcwd()
        self.max_memory_mb = max_memory_mb
        self.max_processes = max_processes
        self.allowed_env_vars = allowed_env_vars or [
            "PATH",
            "HOME",
            "TERM",
            "LANG",
            "SYSTEMROOT",
            "SYSTEMDRIVE",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "USERNAME",
        ]
        self.use_low_integrity = use_low_integrity
        self.shell_path = shell_path
        self._workspace_stamped = False

    # ------------------------------------------------------------------
    # Helper script management
    # ------------------------------------------------------------------

    @classmethod
    def _get_helper_path(cls) -> str:
        """Return path to the integrity-lowering helper script (written once)."""
        if cls._helper_path is None or not os.path.exists(cls._helper_path):
            fd, path = tempfile.mkstemp(suffix=".py", prefix="aar_sandbox_")
            with os.fdopen(fd, "w") as f:
                f.write(_WINDOWS_INTEGRITY_HELPER)
            cls._helper_path = path
            logger.debug("WindowsSubprocessSandbox: helper written to %s", path)
        return cls._helper_path

    # ------------------------------------------------------------------
    # icacls workspace stamping (one-time setup)
    # ------------------------------------------------------------------

    def _stamp_workspace_integrity(self) -> None:
        """Grant Low-integrity write access to the workspace via icacls."""
        if self._workspace_stamped:
            return
        self._workspace_stamped = True
        try:
            import subprocess as _sp

            _sp.run(
                ["icacls", self.workspace, "/setintegritylevel", "(OI)(CI)Low"],
                check=False,
                capture_output=True,
            )
            logger.debug("WorkspaceSandbox: stamped %s as Low-integrity-writable", self.workspace)
        except Exception as exc:
            logger.debug("WorkspaceSandbox: icacls stamp failed (non-fatal): %s", exc)

    # ------------------------------------------------------------------
    # Environment builder
    # ------------------------------------------------------------------

    def _build_env(self, extra: dict[str, str] | None) -> dict[str, str]:
        env = {k: os.environ[k] for k in self.allowed_env_vars if k in os.environ}
        if extra:
            env.update(extra)
        return env

    # ------------------------------------------------------------------
    # Job Object helpers (ctypes kernel32)
    # ------------------------------------------------------------------

    def _assign_job_object(self, pid: int) -> object | None:
        """Create a Job Object, configure limits, assign *pid* to it.

        Returns the job handle on success (caller must close it), or None.
        """
        try:
            import ctypes
            import ctypes.wintypes as wt

            KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)

            JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
            JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
            PROCESS_ALL_ACCESS = 0x1F0FFF
            JobObjectExtendedLimitInformation = 9

            job = KERNEL32.CreateJobObjectW(None, None)
            if not job:
                logger.warning("Job Object: CreateJobObjectW failed (%d)", ctypes.get_last_error())
                return None

            # Build JOBOBJECT_EXTENDED_LIMIT_INFORMATION via ctypes structures.
            # c_int64 covers LARGE_INTEGER (8 bytes); c_size_t covers SIZE_T / ULONG_PTR.
            class _BasicLimit(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wt.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wt.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wt.DWORD),
                    ("SchedulingClass", wt.DWORD),
                ]

            class _IoCounters(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_uint64),
                    ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64),
                    ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64),
                    ("OtherTransferCount", ctypes.c_uint64),
                ]

            class _ExtendedLimit(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _BasicLimit),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            info = _ExtendedLimit()
            info.BasicLimitInformation.LimitFlags = (
                JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                | JOB_OBJECT_LIMIT_PROCESS_MEMORY
                | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            info.BasicLimitInformation.ActiveProcessLimit = self.max_processes
            info.ProcessMemoryLimit = self.max_memory_mb * 1024 * 1024

            KERNEL32.SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )

            proc_handle = KERNEL32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
            if not proc_handle:
                KERNEL32.CloseHandle(job)
                logger.warning(
                    "Job Object: OpenProcess failed for pid %d (%d)",
                    pid,
                    ctypes.get_last_error(),
                )
                return None

            if not KERNEL32.AssignProcessToJobObject(job, proc_handle):
                logger.warning(
                    "Job Object: AssignProcessToJobObject failed (%d)", ctypes.get_last_error()
                )

            KERNEL32.CloseHandle(proc_handle)
            return job

        except Exception as exc:
            logger.warning("Job Object: setup failed (non-fatal): %s", exc)
            return None

    def _close_job(self, job: object) -> None:
        """Close the Job Object handle (triggers KILL_ON_JOB_CLOSE for orphans)."""
        try:
            import ctypes

            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(job)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # execute
    # ------------------------------------------------------------------

    async def execute(
        self,
        command: str,
        timeout: int = 30,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        work_dir = cwd or self.workspace
        proc_env = self._build_env(env)
        self._stamp_workspace_integrity()

        if self.use_low_integrity:
            result = await self._execute_low_integrity(command, timeout, work_dir, proc_env)
            if result is not None:
                return result
            logger.warning(
                "WindowsSubprocessSandbox: Low Integrity execution unavailable; "
                "falling back to Job Object only"
            )

        return await self._execute_with_job_object(command, timeout, work_dir, proc_env)

    async def _execute_with_job_object(
        self,
        command: str,
        timeout: int,
        work_dir: str,
        proc_env: dict[str, str],
    ) -> SandboxResult:
        proc = await _create_subprocess(command, work_dir, proc_env, shell_path=self.shell_path)
        job = self._assign_job_object(proc.pid)
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            if job is not None:
                self._close_job(job)
            await proc.communicate()
            return SandboxResult(timed_out=True, exit_code=-1)
        finally:
            if job is not None:
                self._close_job(job)

        return SandboxResult(
            stdout=stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else "",
            stderr=stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else "",
            exit_code=proc.returncode or 0,
        )

    async def _execute_low_integrity(
        self,
        command: str,
        timeout: int,
        work_dir: str,
        proc_env: dict[str, str],
    ) -> SandboxResult | None:
        """Run command via the Low Integrity helper. Returns None if unavailable."""
        try:
            helper = self._get_helper_path()
            shell = self.shell_path or "bash"
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                helper,
                shell,
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work_dir,
                env=proc_env,
            )
            job = self._assign_job_object(proc.pid)
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                if job is not None:
                    self._close_job(job)
                await proc.communicate()
                return SandboxResult(timed_out=True, exit_code=-1)
            finally:
                if job is not None:
                    self._close_job(job)

            return SandboxResult(
                stdout=stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else "",
                stderr=stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else "",
                exit_code=proc.returncode or 0,
            )
        except Exception as exc:
            logger.warning("WindowsSubprocessSandbox: low integrity helper failed: %s", exc)
            return None

    async def close(self) -> None:
        """Clean up the helper script if present."""
        path = WindowsSubprocessSandbox._helper_path
        if path and os.path.exists(path):
            try:
                os.unlink(path)
                WindowsSubprocessSandbox._helper_path = None
            except OSError:
                pass


class WslDistroSandbox(Sandbox):
    """Windows sandbox: execute commands inside a dedicated WSL2 distro.

    Commands run via ``wsl -d <distro> -- <shell> -c <command>`` so they are
    fully isolated from the user's main WSL2 environment.  The workspace is
    exposed to the distro through the standard WSL2 DrvFs mounts
    (``/mnt/<drive>/...``), so files written by the agent are immediately
    visible from Windows and vice-versa.

    **Isolation provided:**
    - Filesystem: distro has its own root — cannot reach the user's main WSL2
      distro or other distros.
    - Package installs go into the distro only; the host Windows Python and the
      user's main WSL2 distro are unaffected.
    - The distro can be wiped and recreated with ``aar sandbox reset``.

    **Limits:**
    - No outbound network restriction (WSL2 distros share the host network).
    - No memory/process cap (unlike the ``windows`` mode Job Object).
    - Requires WSL2 and the target distro to be set up first
      (``aar sandbox setup``).

    The ``shell_path`` concept from other sandbox modes does not apply here —
    the execution path is always ``wsl -d <distro> -- <shell> -c <cmd>``.
    Use ``shell`` to choose which binary inside the distro runs the command
    (default: ``sh``, works on minimal Alpine without bash installed).
    """

    def __init__(
        self,
        distro_name: str = "aar-sandbox",
        workspace: str | None = None,
        shell: str = "sh",
        allowed_env_vars: list[str] | None = None,
    ) -> None:
        self.distro_name = distro_name
        self.workspace = workspace or os.getcwd()
        self.shell = shell
        self.allowed_env_vars = allowed_env_vars or ["PATH", "HOME", "TERM", "LANG"]

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _to_wsl_path(self, path: str) -> str:
        """Translate a Windows absolute path to its WSL DrvFs mount point.

        Examples::

            "B:\\foo\\bar"  -> "/mnt/b/foo/bar"
            "C:\\Users\\x"  -> "/mnt/c/Users/x"
            "/mnt/b/foo"    -> "/mnt/b/foo"   (already a WSL path — returned as-is)
        """
        # Already a Unix-style path — return unchanged
        if path.startswith("/"):
            return path
        from pathlib import PureWindowsPath

        p = PureWindowsPath(path)
        if p.drive:
            drive_letter = p.drive[0].lower()  # "B:" -> "b"
            # p.parts[1:] skips the drive component; join with forward slashes
            rest = "/".join(p.parts[1:]) if len(p.parts) > 1 else ""
            return f"/mnt/{drive_letter}/{rest}" if rest else f"/mnt/{drive_letter}"
        return path

    # ------------------------------------------------------------------
    # execute
    # ------------------------------------------------------------------

    async def execute(
        self,
        command: str,
        timeout: int = 30,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        work_dir = self._to_wsl_path(cwd or self.workspace)

        # Build optional env-var prefix: "KEY=value KEY2=value2 "
        env_prefix = ""
        if env:
            env_prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items()) + " "

        full_cmd = f"cd {shlex.quote(work_dir)} && {env_prefix}{command}"

        try:
            proc = await asyncio.create_subprocess_exec(
                "wsl",
                "-d",
                self.distro_name,
                "--",
                self.shell,
                "-c",
                full_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return SandboxResult(
                stderr="wsl.exe not found — WSL2 is not available on this system.",
                exit_code=1,
            )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return SandboxResult(timed_out=True, exit_code=-1)

        return SandboxResult(
            stdout=stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else "",
            stderr=stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else "",
            exit_code=proc.returncode or 0,
        )
