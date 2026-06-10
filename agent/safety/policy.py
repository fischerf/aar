"""Safety policy engine — declarative rules for tool execution."""

from __future__ import annotations

import fnmatch
import logging
import re
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agent.tools.schema import SideEffect, ToolSpec

logger = logging.getLogger(__name__)


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"  # requires human approval


class PathRule(BaseModel):
    """A rule matching file paths."""

    pattern: str  # glob pattern, e.g. "/etc/**" or "*.py"
    allow_read: bool = True
    allow_write: bool = False


class CommandRule(BaseModel):
    """A rule matching shell commands."""

    pattern: str  # regex or substring
    decision: PolicyDecision = PolicyDecision.DENY
    is_regex: bool = False


class PolicyConfig(BaseModel):
    """Declarative safety policy configuration."""

    # Global mode
    read_only: bool = False
    require_approval_for_writes: bool = False
    require_approval_for_execute: bool = False

    # Path rules (evaluated in order, first match wins)
    path_rules: list[PathRule] = Field(default_factory=list)

    # Default path restrictions
    denied_paths: list[str] = Field(
        default_factory=lambda: [
            "/etc/shadow",
            "/etc/passwd",
            "**/.env",
            "**/.env.*",
            "**/credentials*",
            "**/secrets*",
            "**/*.pem",
            "**/*.key",
        ]
    )
    allowed_paths: list[str] = Field(default_factory=list)  # empty = allow all not denied

    # Command rules (evaluated in order, first match wins)
    command_rules: list[CommandRule] = Field(default_factory=list)

    # Default denied command patterns
    denied_commands: list[str] = Field(
        default_factory=lambda: [
            # Filesystem destruction
            "rm -rf /",
            "rm -rf /*",
            "rm -rf ~",
            "mkfs",
            "dd if=",
            "> /dev/sda",
            # System control
            "shutdown",
            "reboot",
            "halt",
            "poweroff",
            "init 0",
            "init 6",
            # Fork bomb
            ":(){:|:&};:",
            # Blanket permission change
            "chmod 777",
            "chmod -R 777",
            # Piped remote-code-execution patterns
            "curl|sh",
            "curl | sh",
            "curl|bash",
            "curl | bash",
            "wget|sh",
            "wget | sh",
            "wget|bash",
            "wget | bash",
            # Netcat reverse shell
            "nc -e",
            "ncat -e",
            # Shell history wipe
            "history -c",
        ]
    )

    # Sandbox mode — used to decide whether bash commands need forced approval
    # when allowed_paths is active.
    #
    # The policy engine enforces allowed_paths for *file tool calls* (read_file,
    # write_file, etc.) regardless of sandbox mode, because those calls pass an
    # explicit path argument that can be checked.
    #
    # Shell commands (bash tool) are different: the policy engine cannot inspect
    # which paths the command will touch at runtime. The only protection is either
    # OS-level write isolation (so the kernel/OS refuses out-of-scope writes) or
    # forcing human approval for every shell call.
    #
    # Modes that provide OS-level write isolation (allowed_paths enforcement is
    # meaningful for bash without forced ASK):
    #   "linux"   — Landlock LSM: kernel refuses writes outside workspace
    #   "windows" — Low Integrity Level: Windows ACL blocks writes outside workspace
    #
    # Modes that do NOT provide OS-level write isolation (forced ASK when
    # allowed_paths is active):
    #   "local"   — no isolation at all
    #   "wsl"     — separate distro but commands run as root with full /mnt/ access;
    #               allowed_paths cannot be enforced by the OS for shell commands
    #   "auto"    — resolved before this config is built, so never seen here
    sandbox_mode: str = "local"

    # Logging
    # Off by default: audit logging records full command strings, which frequently
    # contain secrets (API keys, tokens, --password=…). Users who need an audit
    # trail can opt in; output is still redacted via _redact_secrets.
    log_all_commands: bool = False
    log_all_file_access: bool = False


# Patterns for values that should be masked in audit logs. Each pattern captures
# a key/prefix in group 1 and a secret-looking value in group 2.
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    # key=value / key:value  (key contains token/secret/pass/api_key/auth/bearer/...)
    re.compile(
        r"(?i)((?:api[_-]?key|secret|token|password|passwd|bearer|auth(?:orization)?)"
        r"\s*[=:]\s*)(\S+)"
    ),
    # --password VALUE  /  --token VALUE
    re.compile(r"(?i)(--(?:api[_-]?key|secret|token|password|passwd|bearer|auth)\s+)(\S+)"),
    # Authorization: Bearer XYZ   (HTTP headers in curl -H etc.)
    re.compile(r"(?i)(Bearer\s+)([A-Za-z0-9._\-]+)"),
    # Long-ish hex/base64 blobs that look like credentials (>=24 chars of [A-Za-z0-9_-])
    re.compile(r"\b([A-Za-z0-9_\-]{32,})\b"),
]


def _redact_secrets(command: str) -> str:
    """Mask secret-looking values in *command* for safe audit logging."""
    redacted = command
    for i, pat in enumerate(_SECRET_PATTERNS):
        if i < 3:
            redacted = pat.sub(lambda m: f"{m.group(1)}***REDACTED***", redacted)
        else:
            # Standalone long tokens — replace the whole match
            redacted = pat.sub("***REDACTED***", redacted)
    return redacted


def _collapse_posix_path(p: Any) -> str:
    """Collapse ``.`` / ``..`` components in an absolute POSIX path."""
    segments: list[str] = []
    for part in p.parts[1:]:  # skip leading "/"
        if part in ("", "."):
            continue
        if part == "..":
            if segments:
                segments.pop()
            continue
        segments.append(part)
    return "/" + "/".join(segments) if segments else "/"


def _collapse_windows_path(drive: str, p: Any) -> str:
    """Collapse components of a Windows path with an already-lowercased *drive* prefix."""
    segments: list[str] = []
    for part in p.parts[1:]:  # skip drive+root (e.g. "C:\\")
        if part in ("", "."):
            continue
        if part == "..":
            if segments:
                segments.pop()
            continue
        segments.append(part.replace("\\", ""))
    return drive + "/" + "/".join(segments) if segments else drive + "/"


class SafetyPolicy:
    """Evaluates tool calls against the configured policy."""

    def __init__(self, config: PolicyConfig | None = None) -> None:
        self.config = config or PolicyConfig()
        self._compiled_command_rules: list[tuple[re.Pattern | str, PolicyDecision]] = []
        self._compile_rules()

    def _compile_rules(self) -> None:
        """Pre-compile regex patterns for command rules."""
        for rule in self.config.command_rules:
            if rule.is_regex:
                self._compiled_command_rules.append((re.compile(rule.pattern), rule.decision))
            else:
                self._compiled_command_rules.append((rule.pattern, rule.decision))

    def check_tool(self, spec: ToolSpec, arguments: dict[str, Any]) -> PolicyDecision:
        """Check whether a tool call is allowed.

        Returns ALLOW, DENY, or ASK.

        Evaluation order (hard gates first, soft approval last):
        1. read_only  → DENY writes/execute unconditionally
        2. path check → DENY if outside denied_paths or allowed_paths whitelist
        3. command check → DENY if matches a denied command pattern
        4. approval   → ASK if require_approval_for_writes/execute is set
        5.            → ALLOW

        Steps 2–3 are hard DENY that cannot be bypassed by approval. This
        means allowed_paths acts as a true sandbox boundary: a write or shell
        command that falls outside it is denied outright, not merely queued
        for human review.
        """
        # 1. Read-only mode — hard deny all mutations
        if self.config.read_only:
            if SideEffect.WRITE in spec.side_effects or SideEffect.EXECUTE in spec.side_effects:
                logger.info("Policy DENY (read-only mode): %s", spec.name)
                return PolicyDecision.DENY

        # 2. Path checks — hard deny for both reads and writes
        if SideEffect.READ in spec.side_effects or SideEffect.WRITE in spec.side_effects:
            path = arguments.get("path", "")
            if path:
                decision = self._check_path(path, SideEffect.WRITE in spec.side_effects)
                if decision != PolicyDecision.ALLOW:
                    return decision

        # 3. Command checks — hard deny for blocked patterns
        if SideEffect.EXECUTE in spec.side_effects:
            command = arguments.get("command", "")
            if command:
                decision = self._check_command(command)
                if decision != PolicyDecision.ALLOW:
                    return decision

        # 4. Approval gates — soft ask (only reached when path/command passed)
        if SideEffect.WRITE in spec.side_effects and self.config.require_approval_for_writes:
            return PolicyDecision.ASK
        if SideEffect.EXECUTE in spec.side_effects:
            if self.config.require_approval_for_execute:
                return PolicyDecision.ASK
            # When allowed_paths is active and the sandbox provides no OS-level
            # write isolation, we cannot verify which paths a shell command will
            # touch at runtime. Force ASK so the user can review the command before
            # it runs.
            #
            # "linux" (Landlock) and "windows" (Low Integrity Level) enforce write
            # restrictions at the kernel/OS level — the OS refuses out-of-scope writes
            # so forced ASK is not needed.
            #
            # "wsl" is intentionally excluded even though it uses a dedicated distro:
            # commands run as root with full /mnt/<drive>/ access to the Windows host
            # filesystem, so allowed_paths cannot be enforced at the OS level for shell
            # commands. Forced ASK keeps the user in the loop.
            #
            # "local" provides no isolation whatsoever.
            _ISOLATED_MODES = {"linux", "windows"}
            if self.config.allowed_paths and self.config.sandbox_mode not in _ISOLATED_MODES:
                logger.info(
                    "Policy ASK (bash unverifiable under allowed_paths with local sandbox): %s",
                    spec.name,
                )
                return PolicyDecision.ASK

        return PolicyDecision.ALLOW

    @staticmethod
    def _normalize_path(path: str) -> str:
        """Normalise *path* for policy comparison.

        - Unix absolute (``/etc/shadow``): backslashes → forward slashes, then
          collapse ``.``/``..`` components so tricks like ``/etc/../etc/passwd``
          still match ``/etc/**``.
        - Windows drive-rooted (``C:\\project\\file.py`` or ``C:/project/...``):
          lowercase the drive letter for stable matching, collapse components.
          We deliberately don't run these through ``Path.resolve()`` — on
          Windows that would prepend the current drive to Unix paths
          (``/etc/shadow`` → ``C:/etc/shadow``), breaking patterns like
          ``/etc/**``.
        - UNC paths (``\\\\server\\share\\file``): backslashes → forward
          slashes; no resolution (resolving against CWD would be wrong).
        - Truly relative paths (``"."``, ``"README.md"``, ``"src/app.py"``):
          resolved against the CWD so whitelist patterns like ``C:/project/**``
          can match.
        """
        from pathlib import PurePosixPath, PureWindowsPath

        # UNC path (\\server\share\...) — absolute, don't resolve
        if path.startswith("\\\\") or path.startswith("//"):
            return path.replace("\\", "/")

        # Unix absolute (/…)
        if path.startswith("/"):
            try:
                return _collapse_posix_path(PurePosixPath(path.replace("\\", "/")))
            except Exception:
                return path.replace("\\", "/")

        # Windows drive-letter path (C:\… or C:/…)
        if len(path) >= 2 and path[1] == ":":
            try:
                p = PureWindowsPath(path)
                drive = p.drive[0].lower() + ":"  # "C:" / "c:" → "c:"
                return _collapse_windows_path(drive, p)
            except Exception:
                return path.replace("\\", "/")

        # Relative path — resolve against CWD
        try:
            return str(Path(path).resolve()).replace("\\", "/")
        except Exception:
            return path.replace("\\", "/")

    def _check_path(self, path: str, is_write: bool) -> PolicyDecision:
        """Check a file path against path rules."""
        norm_path = self._normalize_path(path)

        # Check explicit path rules first
        for rule in self.config.path_rules:
            norm_pattern = rule.pattern.replace("\\", "/")
            if fnmatch.fnmatch(norm_path, norm_pattern):
                if is_write and not rule.allow_write:
                    logger.info("Policy DENY (path rule, no write): %s", path)
                    return PolicyDecision.DENY
                if not is_write and not rule.allow_read:
                    logger.info("Policy DENY (path rule, no read): %s", path)
                    return PolicyDecision.DENY
                return PolicyDecision.ALLOW

        # Check denied paths (patterns use forward slashes; path is already normalised)
        for pattern in self.config.denied_paths:
            norm_pattern = pattern.replace("\\", "/")
            if fnmatch.fnmatch(norm_path, norm_pattern):
                logger.info("Policy DENY (denied path): %s matches %s", path, pattern)
                return PolicyDecision.DENY

        # Check allowed paths (if specified, only matching paths are permitted)
        if self.config.allowed_paths:
            for pattern in self.config.allowed_paths:
                norm_pattern = pattern.replace("\\", "/")
                if fnmatch.fnmatch(norm_path, norm_pattern):
                    return PolicyDecision.ALLOW
                # Also allow the workspace root directory itself.
                # e.g. pattern "b:/proj/**" should permit list_directory(".")
                # which resolves to "b:/proj" — strip the trailing /** to compare.
                if norm_pattern.endswith("/**"):
                    base = norm_pattern[:-3]  # remove trailing /**
                    # Case-insensitive: Windows drive letters can differ in
                    # case between Path.cwd() and Path.resolve().
                    if norm_path.lower() == base.lower():
                        return PolicyDecision.ALLOW
            logger.info("Policy DENY (not in allowed paths): %s", path)
            return PolicyDecision.DENY

        return PolicyDecision.ALLOW

    def _check_command(self, command: str) -> PolicyDecision:
        """Check a shell command against command rules."""
        if self.config.log_all_commands:
            logger.info("Command audit: %s", _redact_secrets(command))

        # Check explicit command rules first
        for pattern, decision in self._compiled_command_rules:
            if isinstance(pattern, re.Pattern):
                if pattern.search(command):
                    logger.info(
                        "Policy %s (command rule): %s", decision.value, _redact_secrets(command)
                    )
                    return decision
            else:
                if pattern in command:
                    logger.info(
                        "Policy %s (command rule): %s", decision.value, _redact_secrets(command)
                    )
                    return decision

        # Check default denied commands
        for denied in self.config.denied_commands:
            if denied in command:
                logger.warning(
                    "Policy DENY (denied command): %s matches %s",
                    _redact_secrets(command),
                    denied,
                )
                return PolicyDecision.DENY

        return PolicyDecision.ALLOW
