"""Tool executor — validates, permission-checks, and runs tools with safety."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any

from agent.core.config import SafetyConfig, ToolConfig
from agent.core.events import ToolCall, ToolResult
from agent.safety.policy import PolicyConfig, PolicyDecision, SafetyPolicy
from agent.safety.permissions import ApprovalCallback, PermissionManager
from agent.safety.sandbox import LocalSandbox, Sandbox, SubprocessSandbox
from agent.tools.registry import ToolRegistry
from agent.tools.schema import SideEffect

logger = logging.getLogger(__name__)


class ToolExecutor:
    """Executes tool calls with policy enforcement, permission gates, and sandboxing."""

    def __init__(
        self,
        registry: ToolRegistry,
        tool_config: ToolConfig,
        safety_config: SafetyConfig | None = None,
        approval_callback: ApprovalCallback | None = None,
    ) -> None:
        self.registry = registry
        self.tool_config = tool_config

        # Build safety policy from config
        sc = safety_config or SafetyConfig()
        policy_cfg = PolicyConfig(
            read_only=sc.read_only,
            require_approval_for_writes=sc.require_approval_for_writes,
            require_approval_for_execute=sc.require_approval_for_execute,
            denied_paths=sc.denied_paths,
            allowed_paths=sc.allowed_paths,
            log_all_commands=sc.log_all_commands,
        )
        self.policy = SafetyPolicy(policy_cfg)
        self.permissions = PermissionManager(approval_callback)
        self.sandbox = _create_sandbox(sc)

    async def execute(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        """Execute a batch of tool calls and return results."""
        results = []
        for tc in tool_calls:
            result = await self._execute_one(tc)
            results.append(result)
        return results

    async def _execute_one(self, tc: ToolCall) -> ToolResult:
        spec = self.registry.get(tc.tool_name)
        if not spec:
            return ToolResult(
                tool_call_id=tc.tool_call_id,
                tool_name=tc.tool_name,
                output=f"Error: unknown tool '{tc.tool_name}'",
                is_error=True,
            )

        if not spec.handler:
            return ToolResult(
                tool_call_id=tc.tool_call_id,
                tool_name=tc.tool_name,
                output=f"Error: tool '{tc.tool_name}' has no handler",
                is_error=True,
            )

        # --- Safety policy check ---
        decision = self.policy.check_tool(spec, tc.arguments)

        if decision == PolicyDecision.DENY:
            return ToolResult(
                tool_call_id=tc.tool_call_id,
                tool_name=tc.tool_name,
                output="Error: blocked by safety policy",
                is_error=True,
            )

        if decision == PolicyDecision.ASK:
            approval = await self.permissions.request_approval(spec, tc)
            if approval == PolicyDecision.DENY:
                return ToolResult(
                    tool_call_id=tc.tool_call_id,
                    tool_name=tc.tool_name,
                    output="Error: tool call denied by user",
                    is_error=True,
                )

        # --- Execute ---
        t_start = time.monotonic()
        try:
            if inspect.iscoroutinefunction(spec.handler):
                output = await asyncio.wait_for(
                    spec.handler(**tc.arguments),
                    timeout=self.tool_config.command_timeout,
                )
            else:
                output = await asyncio.wait_for(
                    asyncio.to_thread(spec.handler, **tc.arguments),
                    timeout=self.tool_config.command_timeout,
                )
            output_str = str(output)
            if len(output_str) > self.tool_config.max_output_chars:
                output_str = output_str[: self.tool_config.max_output_chars] + "\n... (truncated)"
            return ToolResult(
                tool_call_id=tc.tool_call_id,
                tool_name=tc.tool_name,
                output=output_str,
                duration_ms=(time.monotonic() - t_start) * 1000,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                tool_call_id=tc.tool_call_id,
                tool_name=tc.tool_name,
                output=f"Error: tool '{tc.tool_name}' timed out after {self.tool_config.command_timeout}s",
                is_error=True,
                duration_ms=(time.monotonic() - t_start) * 1000,
            )
        except Exception as e:
            logger.exception("Tool execution error: %s", tc.tool_name)
            return ToolResult(
                tool_call_id=tc.tool_call_id,
                tool_name=tc.tool_name,
                output=f"Error: {type(e).__name__}: {e}",
                is_error=True,
                duration_ms=(time.monotonic() - t_start) * 1000,
            )


def _create_sandbox(config: SafetyConfig) -> Sandbox:
    if config.sandbox == "subprocess":
        return SubprocessSandbox(max_memory_mb=config.sandbox_max_memory_mb)
    return LocalSandbox()
