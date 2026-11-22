"""Built-in PowerShell tool — a native Windows shell that needs no WSL.

A separate tool rather than a mode of ``bash``: a model handed a tool named
``bash`` writes bash, so PowerShell gets its own name and an LLM-facing
description that says which syntax to use.

Invocation
----------
``<exe> -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass
-OutputFormat Text -Command <bootstrap>`` with the model's script on **stdin**:

- The script never appears on the command line, so there is no 32 KB
  command-line limit, no quoting problem, and no ``-EncodedCommand`` (whose
  stderr turns into ``#< CLIXML`` in Windows PowerShell 5.1, and which
  endpoint protection often flags).
- The bootstrap reads stdin as UTF-8, sets ``[Console]::OutputEncoding`` to
  UTF-8 (no BOM) so umlauts survive Windows PowerShell 5.1's OEM console code
  page, and runs the script as the function ``Invoke-AarCommand`` — so a
  ``Write-Error`` reads ``Invoke-AarCommand : …`` rather than echoing the
  whole script back.
- Output is streamed through ``Out-String -Stream -Width 4096`` and
  right-trimmed: tables are neither cut at 120 columns nor padded with spaces,
  and the formatter is flushed before the process exits (a bare ``exit`` after
  ``-Command`` output can otherwise drop the pending table).
- The exit code follows the last statement, like ``bash -c``: ``0`` if it
  succeeded, else the last native ``$LASTEXITCODE``, else ``1``; an explicit
  ``exit N`` wins.
- ``-ExecutionPolicy Bypass`` applies to this process only. Execution policy
  does not govern a script block built from a string anyway; it matters for
  any ``.ps1`` the script itself calls.
"""

from __future__ import annotations

import logging
import os
import shutil

from agent.safety.sandbox import LocalSandbox, Sandbox
from agent.tools.registry import ToolRegistry
from agent.tools.schema import SideEffect, ToolSpec

logger = logging.getLogger(__name__)

TOOL_NAME = "powershell"

# Same bound as bash (see ``shell.DEFAULT_TIMEOUT_HARD_CAP``); duplicated to
# keep this module importable from ``shell``.
DEFAULT_TIMEOUT_HARD_CAP = 3600

POWERSHELL_FLAGS: tuple[str, ...] = (
    "-NoLogo",
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy",
    "Bypass",
    "-OutputFormat",
    "Text",
)

# One line, no double quotes and no backslashes, so Windows argv quoting
# passes it through unchanged. Variables are ``$__aar*`` to stay out of the
# way of the model's script.
_BOOTSTRAP_STATEMENTS: tuple[str, ...] = (
    "$ErrorActionPreference='Continue'",
    "$ProgressPreference='SilentlyContinue'",
    "$__aarUtf8=New-Object System.Text.UTF8Encoding $false",
    "try{[Console]::OutputEncoding=$__aarUtf8}catch{}",
    "$OutputEncoding=$__aarUtf8",
    "if($PSStyle){$PSStyle.OutputRendering='PlainText'}",
    "if(-not $env:PYTHONIOENCODING){$env:PYTHONIOENCODING='utf-8'}",
    "$__aarIn=New-Object System.IO.StreamReader([Console]::OpenStandardInput(),$__aarUtf8)",
    "$__aarSrc=$__aarIn.ReadToEnd()",
    "$__aarIn.Dispose()",
    (
        "try{${function:Invoke-AarCommand}=[scriptblock]::Create($__aarSrc+[char]10+'$global:__aarOk=$?')}"
        "catch{[Console]::Error.WriteLine($_.Exception.InnerException.Message);exit 1}"
    ),
    "$global:__aarOk=$true",
    "$global:LASTEXITCODE=0",
    "Invoke-AarCommand|Out-String -Stream -Width 4096|ForEach-Object{$_.TrimEnd()}",
    "if($global:__aarOk){exit 0}",
    "if($LASTEXITCODE){exit $LASTEXITCODE}",
    "exit 1",
)
BOOTSTRAP = ";".join(_BOOTSTRAP_STATEMENTS)


def is_windows_host() -> bool:
    """True on Windows (a function so tests can simulate either host)."""
    return os.name == "nt"


def _windows_powershell_fallback() -> str | None:
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    path = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return path if os.path.isfile(path) else None


def resolve_powershell_executable(setting: str = "auto") -> str | None:
    """Return the PowerShell executable to run, or ``None`` if there is none.

    ``"auto"`` prefers PowerShell 7 (``pwsh``) and falls back to Windows
    PowerShell 5.1 (``powershell.exe``), on Windows only — on other systems
    ``auto`` resolves to nothing so the tool appears there only when
    configured.  Any other value is a program name looked up on ``PATH`` or a
    path to the executable.
    """
    setting = (setting or "auto").strip()
    if setting.lower() == "auto":
        if not is_windows_host():
            return None
        for name in ("pwsh", "powershell"):
            found = shutil.which(name)
            if found:
                return found
        return _windows_powershell_fallback()
    found = shutil.which(setting)
    if found:
        return found
    return setting if os.path.isfile(setting) else None


def is_windows_powershell(executable: str) -> bool:
    """True for Windows PowerShell 5.1 (``powershell.exe``), False for pwsh."""
    name = executable.replace("\\", "/").rsplit("/", 1)[-1].lower()  # either OS's separator
    return name in ("powershell", "powershell.exe")


def build_powershell_argv(executable: str) -> list[str]:
    """Return the argv that runs a script piped to stdin (see module docstring)."""
    return [executable, *POWERSHELL_FLAGS, "-Command", BOOTSTRAP]


def encode_script(command: str) -> bytes:
    """Encode the model's script for the bootstrap's UTF-8 stdin reader."""
    return command.removeprefix("\ufeff").encode("utf-8", errors="replace")


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n") if text else text


def _describe(executable: str, default_timeout: int, cap: int) -> tuple[str, list[str]]:
    """Build the LLM-facing description and prompt guidelines."""
    windows_ps = is_windows_powershell(executable)
    edition = "Windows PowerShell 5.1" if windows_ps else "PowerShell 7 (pwsh)"
    where = "this Windows computer" if is_windows_host() else "this computer"
    chaining = (
        "Separate commands with ';' — '&&' and '||' do not exist in Windows PowerShell 5.1"
        " (use 'if ($?) { … }')."
        if windows_ps
        else "Separate commands with ';' (or '&&' / '||')."
    )
    description = (
        f"Run a PowerShell command or script on {where} ({edition}). "
        "This is PowerShell, NOT bash: use cmdlets such as Get-ChildItem, Get-Content, "
        "Set-Content, Copy-Item, Move-Item, Remove-Item, Select-String, and $env:NAME for "
        f"environment variables. {chaining} Returns stdout, stderr, and exit code. "
        "Non-interactive: anything that prompts for input fails. "
        f"Default timeout: {default_timeout}s — increase for slow commands (maximum {cap}s)."
    )
    guidelines = [
        (
            "powershell runs PowerShell, not bash: Unix forms such as `ls -la`, `rm -rf`, "
            "`grep`, `cat <<EOF`, `export X=1` fail. Use `Get-ChildItem -Force`, "
            "`Remove-Item -Recurse -Force`, `Select-String`, `Set-Content`, `$env:X = '1'`."
        ),
        (
            "Each powershell call is a fresh process: variables and `Set-Location` do not carry "
            "over to the next call. Quote paths that contain spaces: 'C:\\Program Files\\…'."
        ),
    ]
    return description, guidelines


def register_powershell_tool(
    registry: ToolRegistry,
    sandbox: Sandbox | None = None,
    executable: str | None = None,
    default_timeout: int = 120,
    hard_cap: int | None = None,
) -> bool:
    """Register the ``powershell`` tool; return False if it cannot run here.

    *executable* is a resolved path (``None`` resolves ``"auto"``).  The tool
    is skipped when no PowerShell is found or when *sandbox* cannot run host
    programs (``wsl`` / ``linux`` modes) — registering a tool that escapes the
    configured sandbox, or that can only fail, would be worse than omitting it.

    Timeout handling mirrors ``bash``: *default_timeout* when the model omits
    ``timeout``, everything clamped to *hard_cap* (``0``/``None`` →
    ``DEFAULT_TIMEOUT_HARD_CAP``).  The process tree is killed on timeout.
    """
    active_sandbox = sandbox if sandbox is not None else LocalSandbox()
    if not active_sandbox.supports_program_exec:
        logger.info(
            "powershell tool not registered: %s does not run host programs",
            type(active_sandbox).__name__,
        )
        return False
    exe = executable or resolve_powershell_executable("auto")
    if not exe:
        logger.info("powershell tool not registered: no PowerShell executable found")
        return False

    cap = hard_cap or DEFAULT_TIMEOUT_HARD_CAP
    effective_default = max(1, min(default_timeout, cap))
    argv = build_powershell_argv(exe)

    async def powershell(command: str, timeout: int = effective_default) -> str:
        """Execute a PowerShell script and return stdout + stderr."""
        # Clamp: ``timeout`` arrives straight from the model (see bash, M1).
        timeout = max(1, min(int(timeout), cap))
        result = await active_sandbox.execute_program(
            argv, timeout=timeout, stdin_data=encode_script(command)
        )
        if result.timed_out:
            return f"Error: command timed out after {timeout}s"
        result.stdout = _normalize_newlines(result.stdout)
        result.stderr = _normalize_newlines(result.stderr)
        return result.output

    description, guidelines = _describe(exe, effective_default, cap)
    registry.add(
        ToolSpec(
            name=TOOL_NAME,
            description=description,
            prompt_snippet="Run a PowerShell command (returns stdout, stderr, exit code)",
            prompt_guidelines=guidelines,
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The PowerShell command or script to run",
                    },
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
            handler=powershell,
        )
    )
    return True
