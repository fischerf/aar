"""Permission system — manages approval state for tool operations."""

from __future__ import annotations

import asyncio
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
        self._auto_approved_patterns: set[str] = set()  # e.g. "read_file:/src/**"
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
        """Grant approval for a tool+argument pattern, e.g. 'bash:git *'."""
        self._auto_approved_patterns.add(pattern)
        logger.info("Auto-approved pattern: %s", pattern)

    def revoke(self, tool_name: str) -> None:
        """Revoke blanket approval for a tool."""
        self._auto_approved_tools.discard(tool_name)

    def is_auto_approved(self, spec: ToolSpec, tc: ToolCall) -> bool:
        """Check if this specific call is already approved."""
        if spec.name in self._auto_approved_tools:
            return True
        # Check patterns like "bash:git *"
        for pattern in self._auto_approved_patterns:
            if ":" in pattern:
                tool_part, arg_part = pattern.split(":", 1)
                if tool_part == spec.name:
                    # Check if any argument value starts with the pattern
                    for val in tc.arguments.values():
                        if isinstance(val, str) and val.startswith(arg_part.rstrip("*")):
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
