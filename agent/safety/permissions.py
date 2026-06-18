"""Permission system — manages approval state for tool operations."""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from enum import Enum
from typing import Awaitable, Callable

from agent.core.events import ToolCall
from agent.safety.policy import PolicyDecision
from agent.tools.schema import ToolSpec

logger = logging.getLogger(__name__)


class ApprovalResult(str, Enum):
    APPROVED = "approved"
    DENIED = "denied"
    APPROVED_ALWAYS = "approved_always"  # remember for this session


# Type for approval callbacks
ApprovalCallback = Callable[[ToolSpec, ToolCall], Awaitable[ApprovalResult]]


# S2 — When a legacy two-part pattern (``tool:value``) is registered we need
# to know which argument name it historically referred to. The pre-S2 code
# matched the value against *any* string argument, which let a pattern like
# ``bash:git *`` auto-approve a ``write_file{path: \"git status\"}`` call.
# This table restores the originally-intended targeting per known tool.
_LEGACY_ARG_FOR_TOOL: dict[str, str] = {
    "bash": "command",
    "acp_terminal": "command",
    "read_file": "path",
    "write_file": "path",
    "edit_file": "path",
    "list_directory": "path",
    "find_files": "path",
    "grep": "path",
}


class PermissionManager:
    """Tracks approval state and gates tool execution.

    When the policy returns ASK, the permission manager checks:
    1. Session-level auto-approvals (from "approve always" responses)
    2. Tool-level blanket approvals
    3. Falls back to the approval callback (human-in-the-loop)
    """

    def __init__(self, approval_callback: ApprovalCallback | None = None) -> None:
        self._approval_callback = approval_callback
        self._auto_approved_tools: set[str] = set()
        # S2 — Patterns are now ``(tool, arg_name, value_pattern)`` triples and
        # matched per-argument with ``fnmatch.fnmatchcase``. The previous
        # ``set[str]`` form (``"tool:value"`` with ``startswith``) auto-approved
        # any tool whose call carried the value in *any* string argument, which
        # was exploitable when a model placed an attacker-controlled string in
        # an unrelated argument (e.g. ``path``).
        self._auto_approved_patterns: set[tuple[str, str, str]] = set()
        # Serialise interactive prompts: when a batch of tool calls is executed
        # in parallel (asyncio.gather), only one approval dialog should be active
        # at a time.  Without this lock every coroutine races past is_auto_approved()
        # before any of them can record an APPROVED_ALWAYS grant, causing concurrent
        # stdin reads that deadlock the event loop.
        self._prompt_lock: asyncio.Lock = asyncio.Lock()

    def auto_approve(self, tool_name: str) -> None:
        """Grant blanket approval for a tool for this session."""
        self._auto_approved_tools.add(tool_name)
        logger.info("Auto-approved tool: %s", tool_name)

    def auto_approve_pattern(self, pattern: str) -> None:
        """Grant approval for a tool+argument pattern.

        Preferred form: ``"tool:arg:value_glob"`` — e.g.
        ``"bash:command:git *"`` to auto-approve any ``bash`` call whose
        ``command`` argument matches ``git *``.

        Legacy form: ``"tool:value"`` — the value is matched against the
        argument historically associated with that tool (see
        ``_LEGACY_ARG_FOR_TOOL``) and a deprecation warning is emitted. The
        old loose semantics ("matches any string argument") are no longer
        supported because they allow trivial bypasses (S2).
        """
        parsed = self._parse_pattern(pattern)
        if parsed is None:
            return
        self._auto_approved_patterns.add(parsed)
        logger.info("Auto-approved pattern: %s:%s:%s", *parsed)

    @staticmethod
    def _parse_pattern(pattern: str) -> tuple[str, str, str] | None:
        """Normalise a pattern string into a ``(tool, arg, value_glob)`` triple.

        Returns None if the pattern can't be parsed (and logs a warning).
        """
        parts = pattern.split(":", 2)
        if len(parts) == 3:
            tool, arg, value = parts
            if not tool or not arg:
                logger.warning("Auto-approve pattern missing tool/arg: %r", pattern)
                return None
            return (tool, arg, value)
        if len(parts) == 2:
            tool, value = parts
            arg = _LEGACY_ARG_FOR_TOOL.get(tool)
            if not arg:
                logger.warning(
                    "Auto-approve pattern %r uses the deprecated two-part form for an "
                    "unknown tool; skipping. Use 'tool:arg:value_glob' instead.",
                    pattern,
                )
                return None
            # S2 — Translate ``startswith`` semantics: the legacy ``bash:git `` form
            # meant "command starts with ``git ``". Convert to a glob so
            # fnmatch behaves the same way: ``git *`` (and a trailing ``*`` if
            # the user didn't already include one).
            glob = value if value.endswith("*") else value + "*"
            logger.warning(
                "Auto-approve pattern %r uses the deprecated 'tool:value' form; "
                "translating to 'tool:arg:value' as %r. Update your config.",
                pattern,
                f"{tool}:{arg}:{glob}",
            )
            return (tool, arg, glob)
        logger.warning("Auto-approve pattern %r has no ':' separator; ignored.", pattern)
        return None

    def revoke(self, tool_name: str) -> None:
        """Revoke blanket approval for a tool."""
        self._auto_approved_tools.discard(tool_name)

    def is_auto_approved(self, spec: ToolSpec, tc: ToolCall) -> bool:
        """Check if this specific call is already approved."""
        if spec.name in self._auto_approved_tools:
            return True
        # S2 — Match the value glob only against the *named* argument so a
        # ``bash:command:git *`` pattern can't auto-approve a different tool
        # call whose ``path`` (or any other arg) happens to look like ``git *``.
        for tool, arg, value_glob in self._auto_approved_patterns:
            if tool != spec.name:
                continue
            val = tc.arguments.get(arg)
            if not isinstance(val, str):
                continue
            if fnmatch.fnmatchcase(val, value_glob):
                return True
        return False

    async def request_approval(self, spec: ToolSpec, tc: ToolCall) -> PolicyDecision:
        """Request human approval for a tool call.

        Returns ALLOW or DENY based on the human's response.

        When multiple tool calls are executed concurrently (e.g. a batch of
        write_file calls), approval prompts are serialised via an internal lock
        so only one interactive dialog is active at a time.  Inside the lock the
        auto-approval state is re-checked so that an APPROVED_ALWAYS answer to
        the first prompt silently passes all subsequent sibling calls.
        """
        # Fast path — already approved, no locking needed.
        if self.is_auto_approved(spec, tc):
            return PolicyDecision.ALLOW

        # No callback = deny by default
        if not self._approval_callback:
            logger.warning("No approval callback configured; denying %s", spec.name)
            return PolicyDecision.DENY

        # Acquire the lock to serialise interactive prompts.  Re-check
        # auto-approval inside the lock: a sibling coroutine that ran first
        # may have recorded APPROVED_ALWAYS while we were waiting.
        async with self._prompt_lock:
            if self.is_auto_approved(spec, tc):
                return PolicyDecision.ALLOW

            result = await self._approval_callback(spec, tc)

            if result == ApprovalResult.APPROVED_ALWAYS:
                self._auto_approved_tools.add(spec.name)
                return PolicyDecision.ALLOW
            elif result == ApprovalResult.APPROVED:
                return PolicyDecision.ALLOW
            else:
                return PolicyDecision.DENY
