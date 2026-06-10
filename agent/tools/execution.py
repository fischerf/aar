"""Tool executor — validates, permission-checks, and runs tools with safety."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import sys
import time

from agent.core.config import SafetyConfig, ToolConfig
from agent.core.events import ToolCall, ToolResult
from agent.safety.permissions import ApprovalCallback, PermissionManager
from agent.safety.policy import PolicyConfig, PolicyDecision, SafetyPolicy
from agent.safety.sandbox import (
    LinuxSandbox,
    LocalSandbox,
    Sandbox,
    WindowsSubprocessSandbox,
    WslDistroSandbox,
)
from agent.tools.registry import ToolRegistry

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
        # Resolve "auto" to the actual platform mode so the policy can make
        # accurate decisions (e.g. forced bash approval on non-isolating modes).
        _raw_mode = sc.sandbox.mode
        if _raw_mode == "auto":
            if os.name == "nt":
                _raw_mode = "windows"
            elif sys.platform.startswith("linux"):
                _raw_mode = "linux"
            else:
                _raw_mode = "local"
        policy_cfg = PolicyConfig(
            read_only=sc.read_only,
            require_approval_for_writes=sc.require_approval_for_writes,
            require_approval_for_execute=sc.require_approval_for_execute,
            denied_paths=sc.denied_paths,
            allowed_paths=sc.allowed_paths,
            sandbox_mode=_raw_mode,
            log_all_commands=sc.log_all_commands,
        )
        self.policy = SafetyPolicy(policy_cfg)
        self.permissions = PermissionManager(approval_callback)
        self.sandbox = _create_sandbox(sc)

    async def execute(self, tool_calls: list[ToolCall], parallel: bool = True) -> list[ToolResult]:
        """Execute a batch of tool calls and return results.

        Args:
            tool_calls: The tool calls to execute.
            parallel: If True and multiple calls are present, execute concurrently.
        """
        if parallel and len(tool_calls) > 1:
            return await asyncio.gather(*(self._execute_one(tc) for tc in tool_calls))
        results = []
        for tc in tool_calls:
            result = await self._execute_one(tc)
            results.append(result)
        return results

    async def _execute_one(self, tc: ToolCall) -> ToolResult:
        spec = self.registry.get(tc.tool_name)
        if not spec:
            return _error_result(tc, "unknown_tool", f"unknown tool '{tc.tool_name}'")

        if not spec.handler:
            return _error_result(tc, "no_handler", f"tool '{tc.tool_name}' has no handler")

        # --- Input validation against schema ---
        if spec.input_schema:
            validation_error = _validate_arguments(tc.arguments, spec.input_schema)
            if validation_error:
                return _error_result(
                    tc,
                    "invalid_arguments",
                    f"invalid arguments for '{tc.tool_name}': {validation_error}",
                )

        # --- Safety policy check ---
        decision = self.policy.check_tool(spec, tc.arguments)

        if decision == PolicyDecision.DENY:
            return _error_result(tc, "blocked", "blocked by safety policy")

        if decision == PolicyDecision.ASK:
            approval = await self.permissions.request_approval(spec, tc)
            if approval == PolicyDecision.DENY:
                return _error_result(tc, "denied", "tool call denied by user")

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
            return _error_result(
                tc,
                "timeout",
                f"tool '{tc.tool_name}' timed out after {self.tool_config.command_timeout}s",
                duration_ms=(time.monotonic() - t_start) * 1000,
            )
        except Exception as e:
            logger.debug("Tool execution error: %s", tc.tool_name, exc_info=True)
            return _error_result(
                tc,
                "exception",
                f"{type(e).__name__}: {e}",
                duration_ms=(time.monotonic() - t_start) * 1000,
            )


# Stable machine-readable categories for ToolResult.output on the error path.
# Format: ``Error [<category>]: <message>``. Clients (ACP, TUI, tests) can
# pattern-match on the bracketed category without parsing free-form text.
_ERROR_CATEGORIES = frozenset(
    {
        "unknown_tool",
        "no_handler",
        "invalid_arguments",
        "blocked",
        "denied",
        "timeout",
        "exception",
    }
)


def _error_result(
    tc: ToolCall,
    category: str,
    message: str,
    *,
    duration_ms: float | None = None,
) -> ToolResult:
    assert category in _ERROR_CATEGORIES, f"unknown error category: {category}"
    kwargs: dict = {
        "tool_call_id": tc.tool_call_id,
        "tool_name": tc.tool_name,
        "output": f"Error [{category}]: {message}",
        "is_error": True,
    }
    if duration_ms is not None:
        kwargs["duration_ms"] = duration_ms
    return ToolResult(**kwargs)


def _validate_arguments(arguments: dict, schema: dict) -> str | None:
    """Validate tool arguments against the JSON schema. Return error message or None."""
    try:
        import jsonschema

        jsonschema.validate(instance=arguments, schema=schema)
    except jsonschema.ValidationError as e:
        return e.message
    except Exception:
        pass  # schema validation is best-effort; don't block execution
    return None


def _create_sandbox(config: SafetyConfig) -> Sandbox:
    sb = config.sandbox
    mode = sb.mode

    if mode == "auto":
        if os.name == "nt":
            mode = "windows"
        elif sys.platform.startswith("linux"):
            mode = "linux"
        else:
            mode = "local"

    if mode == "wsl":
        return WslDistroSandbox(
            distro_name=sb.wsl.distro,
            workspace=sb.wsl.workspace,
            shell=sb.wsl.shell,
            wsl_user=sb.wsl.wsl_user,
            restrict_to_workspace=sb.wsl.restrict_to_workspace,
        )
    if mode == "linux":
        return LinuxSandbox(
            workspace=sb.linux.workspace,
            max_memory_mb=sb.linux.max_memory_mb,
        )
    if mode == "windows":
        return WindowsSubprocessSandbox(
            workspace=sb.windows.workspace,
            max_memory_mb=sb.windows.max_memory_mb,
            max_processes=sb.windows.max_processes,
            use_low_integrity=sb.windows.use_low_integrity,
        )
    return LocalSandbox()
