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

    # Logging
    log_all_commands: bool = True
    log_all_file_access: bool = False


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
        """
        # Read-only mode blocks all writes and executions
        if self.config.read_only:
            if SideEffect.WRITE in spec.side_effects or SideEffect.EXECUTE in spec.side_effects:
                logger.info("Policy DENY (read-only mode): %s", spec.name)
                return PolicyDecision.DENY

        # Check side-effect-based approval requirements
        if SideEffect.WRITE in spec.side_effects and self.config.require_approval_for_writes:
            return PolicyDecision.ASK
        if SideEffect.EXECUTE in spec.side_effects and self.config.require_approval_for_execute:
            return PolicyDecision.ASK

        # Path checks for file tools
        if SideEffect.READ in spec.side_effects or SideEffect.WRITE in spec.side_effects:
            path = arguments.get("path", "")
            if path:
                decision = self._check_path(path, SideEffect.WRITE in spec.side_effects)
                if decision != PolicyDecision.ALLOW:
                    return decision

        # Command checks for shell tools
        if SideEffect.EXECUTE in spec.side_effects:
            command = arguments.get("command", "")
            if command:
                decision = self._check_command(command)
                if decision != PolicyDecision.ALLOW:
                    return decision

        return PolicyDecision.ALLOW

    @staticmethod
    def _normalize_path(path: str) -> str:
        """Normalise *path* for policy comparison.

        - Paths that are already absolute — Unix-style (``/etc/shadow``) or
          Windows drive-rooted (``C:\\project\\file.py``) — are left as-is
          with only their separators converted to ``/``.  Feeding them through
          ``Path.resolve()`` on Windows would prepend the current drive letter
          (``/etc/shadow`` → ``C:/etc/shadow``), which breaks patterns written
          as ``/etc/**`` and the tests that use them.
        - Truly relative paths (``"."``, ``"README.md"``, ``"src/app.py"``)
          ARE resolved against the CWD so they can be matched against
          whitelist patterns that contain the full absolute CWD
          (e.g. ``C:/project/**``).
        """
        # Unix absolute (/…) or Windows drive-letter path (C:\… or C:/…)
        if path.startswith("/") or (len(path) >= 2 and path[1] == ":"):
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
            logger.info("Command audit: %s", command)

        # Check explicit command rules first
        for pattern, decision in self._compiled_command_rules:
            if isinstance(pattern, re.Pattern):
                if pattern.search(command):
                    logger.info("Policy %s (command rule): %s", decision.value, command)
                    return decision
            else:
                if pattern in command:
                    logger.info("Policy %s (command rule): %s", decision.value, command)
                    return decision

        # Check default denied commands
        for denied in self.config.denied_commands:
            if denied in command:
                logger.warning("Policy DENY (denied command): %s matches %s", command, denied)
                return PolicyDecision.DENY

        return PolicyDecision.ALLOW
