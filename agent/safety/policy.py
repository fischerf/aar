"""Safety policy engine — declarative rules for tool execution."""

from __future__ import annotations

import fnmatch
import logging
import os
import posixpath
import re
import shlex
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

    # Read-only allowlist — paths the agent may *read* but never *write*.
    # Evaluated after denied_paths (so credential patterns still win) but
    # before the allowed_paths whitelist's hard deny, so a matching read is
    # permitted even when allowed_paths would otherwise exclude it. Writes
    # are never granted here and fall through to the allowed_paths check.
    #
    # Used to grant access to skill files discovered outside the workspace
    # (e.g. ~/.aar/skills) so the model can load skill instructions even when
    # allowed_paths restricts it to the project directory. Unlike path_rules
    # (which are evaluated first and would shadow denied_paths), this cannot be
    # used to read a credential file that happens to live under a skills dir.
    read_only_paths: list[str] = Field(default_factory=list)

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
            # Piped remote-code-execution is covered by
            # ``denied_command_patterns`` below — the literal forms
            # ("curl | sh", …) never appeared in a real one-liner.
            # Netcat reverse shell
            "nc -e",
            "ncat -e",
            # Shell history wipe
            "history -c",
        ]
    )

    # H1 — Regex deny-list, evaluated against the raw command string.
    # Shell metacharacters can't be modelled structurally by ``shlex``, so
    # download-and-execute and fork-bomb shapes are matched with regexes
    # instead of the literal substrings that never fired in practice.
    # Set to ``[]`` to disable.
    denied_command_patterns: list[str] = Field(
        default_factory=lambda: [
            # curl/wget … | [sudo] sh|bash|zsh|dash|ksh
            r"\b(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+|doas\s+)?"
            r"(?:/usr/bin/|/bin/|/usr/local/bin/)?(?:ba|z|da|k|a)?sh\b",
            # classic fork bomb, with or without whitespace
            r":\(\)\s*\{\s*:\s*\|\s*:?\s*&\s*\}\s*;\s*:",
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


# S1 — Argument names that conventionally carry a filesystem path. The
# policy engine inspects the tool's JSON schema and runs ``_check_path`` on
# every property whose name appears here, ends with ``_path``, or is
# annotated ``format: \"path\"``. Pre-S1 only the literal ``\"path\"`` key
# was checked, so tools with ``source_path`` / ``destination_path`` /
# ``directory`` etc. bypassed ``allowed_paths`` and ``denied_paths``
# entirely.
_PATH_ARG_NAMES = frozenset({"path", "filepath", "directory", "cwd"})


def _is_path_property(name: str, prop_schema: dict[str, Any] | None) -> bool:
    """Return True if a schema property describes a filesystem path."""
    if name in _PATH_ARG_NAMES or name.endswith("_path"):
        return True
    if isinstance(prop_schema, dict) and prop_schema.get("format") == "path":
        return True
    return False


def _iter_path_args(spec: ToolSpec, arguments: dict[str, Any]):
    """Yield ``(arg_name, value)`` pairs for every path-like argument.

    Schema-driven: walks ``spec.input_schema['properties']`` and yields any
    property whose name or ``format`` annotation marks it as a path, provided
    the call carries a non-empty string value for that argument.

    When the spec has no schema (e.g. MCP tools registered without one), this
    falls back to the legacy ``arguments['path']`` lookup so we still get
    *some* protection.
    """
    schema = spec.input_schema if isinstance(spec.input_schema, dict) else None
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if isinstance(properties, dict):
        for name, prop_schema in properties.items():
            if not _is_path_property(name, prop_schema):
                continue
            val = arguments.get(name)
            if isinstance(val, str) and val:
                yield name, val
        return
    # Fallback: no schema — keep the legacy single-arg check.
    val = arguments.get("path")
    if isinstance(val, str) and val:
        yield "path", val


# S3 — Shell metacharacters that shlex cannot evaluate structurally.
# Deny-list patterns containing any of these fall back to substring matching
# so legacy rules like ``curl|sh`` or ``> /dev/sda`` still fire when the
# literal text appears in the command.
_SHELL_METACHARS = frozenset("|<>;&`$()")


# H1 — Wrappers that delegate to another program. The deny-list has to look
# *through* them: ``sudo shutdown`` is exactly as final as ``shutdown``.
_WRAPPERS = frozenset(
    {
        "sudo",
        "doas",
        "env",
        "nohup",
        "time",
        "nice",
        "ionice",
        "xargs",
        "command",
        "exec",
        "busybox",
        "stdbuf",
        "setsid",
        "timeout",
    }
)

# Shells whose ``-c <string>`` argument is itself a command line to inspect.
_INNER_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash"})

# Token characters that separate one simple command from the next.
_SEPARATOR_CHARS = ";&|"

# ``rm`` long options worth normalising into their short equivalents.
_RM_LONG_FLAGS = {"--recursive": "r", "--force": "f", "--dir": "d", "--no-preserve-root": "R"}

# Wrapper options that consume the following token as their value, so the
# value isn't mistaken for the wrapped command (``nice -n 10 halt``).
_WRAPPER_VALUE_FLAGS = frozenset(
    {"-n", "-c", "-u", "-i", "-I", "-P", "-L", "-s", "-k", "-p", "-g", "-a"}
)

# Wrappers whose first positional argument is not the command (``timeout 5 cmd``).
_WRAPPER_POSITIONAL_ARGS = {"timeout": 1}

_MAX_UNWRAP_DEPTH = 4


def _basename(token: str) -> str:
    return posixpath.basename(token.replace("\\", "/"))


def _simple_commands(command: str) -> list[list[str]] | None:
    """Split *command* into simple commands on ``; && || | &`` and newlines.

    H1 — The old check only ever looked at the first token sequence, so
    ``true; shutdown -h now`` and ``echo x && rm -rf /`` sailed past the
    deny-list. Returns ``None`` when the command cannot be tokenised, which
    the caller treats as "fall back to substring matching".
    """
    commands: list[list[str]] = []
    for line in command.splitlines():
        if not line.strip():
            continue
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=_SEPARATOR_CHARS)
            lexer.whitespace_split = True
            lexer.commenters = ""  # '#' is data here, not a comment
            tokens = list(lexer)
        except ValueError:
            return None
        current: list[str] = []
        for token in tokens:
            if token and all(ch in _SEPARATOR_CHARS for ch in token):
                if current:
                    commands.append(current)
                    current = []
            else:
                current.append(token)
        if current:
            commands.append(current)
    return commands


def _unwrap(tokens: list[str], depth: int = 0) -> list[list[str]]:
    """Return *tokens* plus every command it delegates to.

    Strips wrapper programs (``sudo``, ``env FOO=bar``, ``xargs``, …) and
    expands ``sh -c '<command>'`` so the deny-list sees the real verb.
    """
    if not tokens:
        return []
    variants: list[list[str]] = [tokens]

    stripped = tokens
    while stripped and _basename(stripped[0]) in _WRAPPERS:
        wrapper = _basename(stripped[0])
        rest = stripped[1:]
        while rest:
            head = rest[0]
            if head.startswith("-"):
                takes_value = head in _WRAPPER_VALUE_FLAGS and len(rest) > 1
                rest = rest[2:] if takes_value else rest[1:]
                continue
            if "=" in head:  # ``env FOO=bar cmd``
                rest = rest[1:]
                continue
            break
        for _ in range(_WRAPPER_POSITIONAL_ARGS.get(wrapper, 0)):
            if rest:
                rest = rest[1:]
        if not rest:
            break
        stripped = rest
        variants.append(stripped)

    if depth >= _MAX_UNWRAP_DEPTH:
        return variants

    target = stripped
    if target and _basename(target[0]) in _INNER_SHELLS and "-c" in target[1:]:
        index = target.index("-c", 1)
        if index + 1 < len(target):
            for inner in _simple_commands(target[index + 1]) or []:
                variants.extend(_unwrap(inner, depth + 1))
    return variants


def _candidate_commands(command: str) -> list[list[str]] | None:
    """Every token list the deny-list should be evaluated against."""
    simple = _simple_commands(command)
    if simple is None:
        return None
    candidates: list[list[str]] = []
    for tokens in simple:
        candidates.extend(_unwrap(tokens))
    return candidates


def _normalize_rm(tokens: list[str]) -> list[str]:
    """Canonicalise ``rm`` flags so ``rm -fr /`` matches the ``rm -rf /`` rule."""
    if not tokens or _basename(tokens[0]) != "rm":
        return tokens
    flags: set[str] = set()
    operands: list[str] = []
    for token in tokens[1:]:
        if token == "--":
            continue
        if token.startswith("--"):
            mapped = _RM_LONG_FLAGS.get(token)
            if mapped:
                flags.add(mapped)
            continue
        if token.startswith("-") and len(token) > 1:
            flags.update(token[1:].replace("R", "r"))
            continue
        operands.append(token)
    normalized = ["rm"]
    if flags:
        normalized.append("-" + "".join(sorted(flags)))
    normalized.extend(operands)
    return normalized


def _denied_matches(
    denied: str,
    raw_command: str,
    cmd_tokens: list[str] | None,
) -> bool:
    """Decide whether *denied* matches *raw_command* under S3 rules.

    - If *cmd_tokens* is None the command failed to tokenise; fall back to
      substring matching for safety (don't let a malformed command escape).
    - If *denied* contains shell metacharacters that shlex cannot model
      structurally, use substring matching.
    - Otherwise tokenise *denied* with shlex and require either an exact
      single-token match against the leading executable (with basename
      fallback) or, for multi-token patterns, a prefix-of-each-token match
      starting at position 0.
    """
    if cmd_tokens is None:
        return denied in raw_command

    if any(ch in _SHELL_METACHARS for ch in denied):
        return denied in raw_command

    try:
        denied_tokens = shlex.split(denied)
    except ValueError:
        # Author wrote something shlex can't parse — fall back to substring.
        return denied in raw_command

    if not denied_tokens or not cmd_tokens:
        return False

    # H1 — ``rm -fr /`` and ``rm -r -f /`` are the same command as ``rm -rf /``;
    # normalise both sides so flag order and spelling don't matter.
    denied_tokens = _normalize_rm(denied_tokens)
    cmd_tokens = _normalize_rm(cmd_tokens)

    # Compare leading executable: exact OR basename match. This catches
    # ``/sbin/shutdown`` matching the denied ``shutdown`` pattern.
    cmd_head = cmd_tokens[0]
    cmd_head_base = _basename(cmd_head)
    denied_head = denied_tokens[0]
    # H1 — also match the dotted tool family (``mkfs`` -> ``mkfs.ext4``), which
    # otherwise evaded a single-token denial by adding a filesystem suffix.
    family_match = len(denied_tokens) == 1 and cmd_head_base.startswith(denied_head + ".")
    if cmd_head != denied_head and cmd_head_base != denied_head and not family_match:
        return False

    # Single-token denial: head match is enough.
    if len(denied_tokens) == 1:
        return True

    # Multi-token denial: each subsequent denied token must be a prefix of
    # the corresponding command token. ``dd if=`` matches ``dd if=/dev/zero``;
    # ``chmod 777`` matches ``chmod 777 -R foo`` but NOT ``chmod 644``.
    if len(cmd_tokens) < len(denied_tokens):
        return False
    for d_tok, c_tok in zip(denied_tokens[1:], cmd_tokens[1:]):
        if not c_tok.startswith(d_tok):
            return False
    return True


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


# S8 — Defend ``allowed_paths`` / ``denied_paths`` against symlink escapes.
# ``_normalize_path`` follows symlinks for *relative* paths (via
# ``Path.resolve()``) but intentionally leaves absolute paths in their
# syntactic form to avoid Windows path-mangling. That asymmetry lets an
# attacker who can place a symlink inside ``allowed_paths`` (cloned repo,
# previously-approved ``ln -s``, an MCP tool) escape the sandbox: an
# absolute path like ``C:\\proj\\link`` lexically matches ``c:/proj/**``
# but ``open()`` follows the symlink to e.g. ``C:\\Users\\me\\.ssh\\id_rsa``.
#
# This helper returns the symlink target *only when* an actual symlink is
# present in the chain, so OS-level normalisation (case, separators) on
# Windows doesn't trigger spurious re-checks.
def _resolve_symlink_target(path: str) -> str | None:
    """Return ``os.path.realpath(path)`` iff a component of *path* is a symlink.

    Returns ``None`` for relative paths (already symlink-resolved by
    ``_normalize_path`` via ``Path.resolve()``), for paths with no symlink
    component, and on any OS error during the walk (fail-open at this
    layer — the lexical check has already run).
    """
    try:
        if not os.path.isabs(path):
            return None
        p = Path(path)
        # Walk from the anchor toward the leaf, checking each prefix. We
        # don't short-circuit on the first non-existent component because
        # the *anchor* always exists; symlinks can sit at any level.
        prefix = Path(p.anchor) if p.anchor else Path(p.parts[0])
        for part in p.parts[1:]:
            prefix = prefix / part
            try:
                if prefix.is_symlink():
                    return os.path.realpath(path)
            except OSError:
                # Permission denied on a component — keep walking; a deeper
                # symlink may still be visible.
                continue
            if not prefix.exists():
                # Remaining components don't exist on disk; no symlink to find.
                return None
        return None
    except (OSError, ValueError):
        return None


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

        self._compiled_denied_patterns: list[re.Pattern] = []
        for pattern in self.config.denied_command_patterns:
            try:
                self._compiled_denied_patterns.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                logger.warning("Ignoring invalid denied_command_pattern %r: %s", pattern, exc)

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

        # 2. Path checks — hard deny for both reads and writes. S1: walk
        # every schema-declared path-like argument, not just the literal
        # ``\"path\"`` key. ``allowed_paths`` and ``denied_paths`` must apply
        # to tools that pass ``source_path``, ``destination_path``,
        # ``directory``, etc.
        if SideEffect.READ in spec.side_effects or SideEffect.WRITE in spec.side_effects:
            is_write = SideEffect.WRITE in spec.side_effects
            for _arg_name, path_val in _iter_path_args(spec, arguments):
                decision = self._check_path(path_val, is_write)
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
        """Check a file path against path rules.

        S8 — Symlink-aware. The lexical form of *path* is checked first;
        if it isn't already denied we also resolve any symlink in the chain
        and re-check the target. Both checks must allow for the call to
        proceed. Without this, an absolute path lexically inside
        ``allowed_paths`` could silently follow a symlink to a file outside
        the sandbox (e.g. ``/etc/shadow``, ``~/.ssh/id_rsa``).
        """
        lex_decision = self._check_path_rules(self._normalize_path(path), path, is_write)
        if lex_decision == PolicyDecision.DENY:
            return lex_decision

        target = _resolve_symlink_target(path)
        if target is not None and target != path:
            target_decision = self._check_path_rules(self._normalize_path(target), target, is_write)
            if target_decision != PolicyDecision.ALLOW:
                logger.info(
                    "Policy DENY (symlink escape): %s -> %s (target=%s)",
                    path,
                    target,
                    target_decision.value,
                )
                return target_decision

        return lex_decision

    def _check_path_rules(
        self, norm_path: str, original_path: str, is_write: bool
    ) -> PolicyDecision:
        """Apply path_rules / denied_paths / read_only_paths / allowed_paths.

        Pure rule evaluation against the already-normalised *norm_path*.
        *original_path* is used only for log messages. Split out from
        ``_check_path`` so S8 can call it twice (lexical + symlink target).
        """
        # Check explicit path rules first
        for rule in self.config.path_rules:
            norm_pattern = rule.pattern.replace("\\", "/")
            if fnmatch.fnmatch(norm_path, norm_pattern):
                if is_write and not rule.allow_write:
                    logger.info("Policy DENY (path rule, no write): %s", original_path)
                    return PolicyDecision.DENY
                if not is_write and not rule.allow_read:
                    logger.info("Policy DENY (path rule, no read): %s", original_path)
                    return PolicyDecision.DENY
                return PolicyDecision.ALLOW

        # Check denied paths (patterns use forward slashes; path is already normalised)
        for pattern in self.config.denied_paths:
            norm_pattern = pattern.replace("\\", "/")
            if fnmatch.fnmatch(norm_path, norm_pattern):
                logger.info("Policy DENY (denied path): %s matches %s", original_path, pattern)
                return PolicyDecision.DENY

        # Read-only allowlist (e.g. discovered skills). Grants reads only —
        # never writes — and is checked after denied_paths so credential
        # patterns still win. A matching read is allowed even when allowed_paths
        # would otherwise exclude it; writes fall through to the checks below.
        if not is_write:
            for pattern in self.config.read_only_paths:
                norm_pattern = pattern.replace("\\", "/")
                if fnmatch.fnmatch(norm_path, norm_pattern):
                    return PolicyDecision.ALLOW

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
            logger.info("Policy DENY (not in allowed paths): %s", original_path)
            return PolicyDecision.DENY

        return PolicyDecision.ALLOW

    def _check_command(self, command: str) -> PolicyDecision:
        """Check a shell command against command rules.

        S3 — The default ``denied_commands`` list is matched by tokenising
        *command* with ``shlex`` instead of doing a naive substring search.
        That eliminates noisy false positives like ``echo \"do not shutdown\"``
        while still catching e.g. ``/sbin/shutdown`` via basename comparison.
        Custom ``command_rules`` (which are explicit, user-authored regex or
        substring rules) are unchanged.

        Best-effort tradeoffs:
          - Patterns built around shell metacharacters (``|``, ``>``, ``;``,
            ``&``) cannot be evaluated structurally by shlex; for those we
            fall back to the legacy substring match so the existing
            ``curl|sh`` / ``> /dev/sda`` style rules still fire when the
            literal substring appears.
          - H1: every simple command is now checked (``;``, ``&&``, ``||``,
            ``|``, ``&`` and newlines split them), known wrappers (``sudo``,
            ``env FOO=bar``, ``xargs``, …) are stripped, and ``sh -c '…'`` is
            expanded one level. A verb hidden inside a *non-shell* interpreter
            (``python -c '…'``, ``perl -e '…'``) still passes, as does anything
            built at runtime from string fragments. The deny-list remains a
            best-effort guardrail: keep ``require_approval_for_execute`` on, or
            run under the WSL sandbox, if you need a real boundary.
        """
        if self.config.log_all_commands:
            logger.info("Command audit: %s", _redact_secrets(command))

        # Check explicit command rules first — these are user-authored and
        # explicit, so we honour the historical substring/regex semantics.
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

        # H1 — Regex deny-list, matched against the raw string. Shell
        # metacharacters can't be modelled structurally, so download-and-execute
        # and fork-bomb shapes are matched here rather than by token.
        for compiled in self._compiled_denied_patterns:
            if compiled.search(command):
                logger.warning(
                    "Policy DENY (denied command pattern): %s matches %s",
                    _redact_secrets(command),
                    compiled.pattern,
                )
                return PolicyDecision.DENY

        # H1 — Evaluate *every* simple command, not just the first, and look
        # through wrappers (``sudo``) and inner shells (``sh -c '…'``).
        # ``None`` means the command didn't tokenise; fall back to substring
        # matching so a malformed command can't bypass the deny-list.
        candidates = _candidate_commands(command)

        for denied in self.config.denied_commands:
            if candidates is None:
                if _denied_matches(denied, command, None):
                    logger.warning(
                        "Policy DENY (denied command): %s matches %s",
                        _redact_secrets(command),
                        denied,
                    )
                    return PolicyDecision.DENY
                continue
            for cmd_tokens in candidates:
                if _denied_matches(denied, command, cmd_tokens):
                    logger.warning(
                        "Policy DENY (denied command): %s matches %s",
                        _redact_secrets(command),
                        denied,
                    )
                    return PolicyDecision.DENY

        return PolicyDecision.ALLOW
