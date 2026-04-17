"""ACP permission bridge — maps ACP approval requests to Aar's ApprovalCallback.

When the safety policy returns ``ASK`` for a tool call, Aar's ``PermissionManager``
invokes the registered ``ApprovalCallback``.  In stdio (Zed) mode the ACP
connection provides a ``request_permission`` coroutine that suspends execution,
sends a structured permission request to the ACP client, and waits for the
human's decision (allow / allow-always / deny).

``make_acp_approval_callback`` wraps that coroutine in the ``ApprovalCallback``
interface so it can be dropped straight into ``AarAgent`` / ``ToolExecutor``
without any other changes to the core.

Usage (wired automatically by ``AarAcpAgent.prompt``)::

    from agent.transports.acp_permissions import make_acp_approval_callback

    callback = make_acp_approval_callback(conn, session_id)
    aar_agent = AarAgent(config=cfg, approval_callback=callback)

The ``conn`` object is the live ACP SDK connection supplied to
``AarAcpAgent.on_connect``.  When there is no active connection (e.g. the HTTP
transport), pass ``None``; a safe fallback (deny) is returned instead.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agent.core.events import ToolCall
from agent.safety.permissions import ApprovalCallback, ApprovalResult
from agent.tools.schema import ToolSpec
from agent.transports.acp.common import _side_effects_to_tool_kind

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Option definitions — sent to the ACP client so it can render a UI
# ---------------------------------------------------------------------------

#: Ordered list of (option_id, kind, label) tuples used to build PermissionOptions.
#: ``option_id`` is what comes back in the response's ``AllowedOutcome.option_id``.
_OPTIONS: list[tuple[str, str, str]] = [
    ("allow_once", "allow_once", "Allow once"),
    ("allow_always", "allow_always", "Allow always"),
    ("deny", "reject_once", "Deny"),
]

# Maps ACP ``option_id`` → Aar ``ApprovalResult``
_OPTION_TO_RESULT: dict[str, ApprovalResult] = {
    "allow_once": ApprovalResult.APPROVED,
    "allow_always": ApprovalResult.APPROVED_ALWAYS,
    "deny": ApprovalResult.DENIED,
}


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def make_acp_approval_callback(
    conn: Any,
    session_id: str,
    timeout: float = 0.0,
) -> ApprovalCallback:
    """Return an ``ApprovalCallback`` that routes approval requests to the ACP client.

    When the ``PermissionManager`` needs a human decision for a tool call it
    awaits this callback.  The callback suspends the agent, sends a
    ``request_permission`` message to the connected ACP client (e.g. Zed),
    and resumes once the client returns a chosen ``PermissionOption``.

    If ``conn`` is ``None`` (no active ACP connection) the callback returns
    ``ApprovalResult.DENIED`` for every request — a safe default that avoids
    executing sensitive operations without an audience.

    Args:
        conn:       Active ACP SDK connection (``acp.interfaces.Client``).
                    Kept typed as ``Any`` to avoid a hard import of the SDK
                    at module load time — the SDK is optional.
        session_id: The ACP session id for the current prompt.
        timeout:    Seconds to wait for a user response before auto-denying.
                    ``0`` (default) means wait indefinitely. Must be ``>= 0``;
                    a negative value would make ``asyncio.wait_for`` raise
                    ``TimeoutError`` immediately and silently deny every
                    request, so it's rejected at factory time.

    Returns:
        An async callable ``(spec, tc) -> ApprovalResult`` compatible with
        ``agent.safety.permissions.ApprovalCallback``.

    Raises:
        ValueError: if ``timeout`` is negative or not a real number.
    """
    import math

    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
        raise ValueError(f"timeout must be a number, got {type(timeout).__name__}")
    if math.isnan(timeout) or math.isinf(timeout):
        raise ValueError(f"timeout must be finite, got {timeout!r}")
    if timeout < 0:
        raise ValueError(f"timeout must be >= 0 (0 means wait indefinitely), got {timeout!r}")

    if conn is None:
        logger.debug("make_acp_approval_callback: no connection — using deny fallback")

        async def _deny_all(spec: ToolSpec, tc: ToolCall) -> ApprovalResult:
            logger.warning(
                "ACP: no active connection; denying tool call '%s' (session %s)",
                tc.tool_name,
                session_id,
            )
            return ApprovalResult.DENIED

        return _deny_all

    async def _callback(spec: ToolSpec, tc: ToolCall) -> ApprovalResult:
        """Suspend execution and ask the ACP client for a permission decision."""
        try:
            from acp.schema import AllowedOutcome, PermissionOption, ToolCallUpdate
        except ImportError:
            logger.error(
                "agent-client-protocol package not installed; denying tool '%s'",
                tc.tool_name,
            )
            return ApprovalResult.DENIED

        options = [
            PermissionOption(option_id=oid, kind=kind, name=label) for oid, kind, label in _OPTIONS
        ]

        kind = _side_effects_to_tool_kind(spec.side_effects)
        # tool_call_id is guaranteed non-empty at ToolCall construction (see
        # the model validator in agent.core.events.ToolCall), so the same id
        # is visible to both on_event (ToolCallStart) and here.
        tool_call_id = tc.tool_call_id

        # Yield once so that any pending session_update tasks (e.g. the
        # ToolCallStart that on_event enqueued) can flush to the client
        # *before* we send the permission request.  This ensures the client
        # sees the tool as "pending" before the approval dialog appears.
        await asyncio.sleep(0)

        # request_permission expects a ToolCallUpdate (the mutable sibling of
        # ToolCallStart).  Using _acp.start_tool_call() would produce a
        # ToolCallStart which fails Pydantic validation inside the SDK.
        acp_tool_call = ToolCallUpdate(
            tool_call_id=tool_call_id,
            title=tc.tool_name,
            kind=kind,
            status="pending",
            raw_input=tc.arguments,
        )

        logger.debug(
            "ACP: requesting permission for tool '%s' (id=%s, kind=%s, session=%s)",
            tc.tool_name,
            tool_call_id,
            kind,
            session_id,
        )

        try:
            response = await asyncio.wait_for(
                conn.request_permission(
                    session_id=session_id,
                    tool_call=acp_tool_call,
                    options=options,
                ),
                timeout=timeout or None,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "ACP: permission request timed out after %.0fs for tool '%s' (session %s)",
                timeout,
                tc.tool_name,
                session_id,
            )
            return ApprovalResult.DENIED
        except Exception as exc:
            logger.warning(
                "ACP: permission request failed for tool '%s' (session %s): %s",
                tc.tool_name,
                session_id,
                exc,
            )
            return ApprovalResult.DENIED

        outcome = response.outcome
        if isinstance(outcome, AllowedOutcome):
            result = _OPTION_TO_RESULT.get(outcome.option_id, ApprovalResult.DENIED)
            logger.info(
                "ACP: permission '%s' → %s for tool '%s' (session %s)",
                outcome.option_id,
                result.value,
                tc.tool_name,
                session_id,
            )
            return result

        # Any non-AllowedOutcome (e.g. RejectedOutcome) is treated as deny.
        logger.info(
            "ACP: permission denied (non-allowed outcome) for tool '%s' (session %s)",
            tc.tool_name,
            session_id,
        )
        return ApprovalResult.DENIED

    return _callback
