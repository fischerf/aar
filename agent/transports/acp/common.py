"""Shared helpers for the ACP transport modules.

Everything here is used by both ``stdio`` and ``http`` and/or by
``acp_permissions``. Keep it dependency-light so the stdio and HTTP paths
can import it without pulling in the other transport's heavy machinery.
"""

from __future__ import annotations

import logging
from typing import Any

from agent.core.config import AgentConfig, load_config
from agent.core.events import ToolCall
from agent.core.state import AgentState
from agent.safety.permissions import ApprovalResult
from agent.tools.schema import SideEffect, ToolSpec

logger = logging.getLogger(__name__)


def _load_default_config() -> AgentConfig:
    from pathlib import Path

    p = Path.home() / ".aar" / "config.json"
    return load_config(p) if p.is_file() else AgentConfig()


async def _auto_approve(spec: ToolSpec, tc: ToolCall) -> ApprovalResult:
    logger.info("ACP transport: auto-approving %s", tc.tool_name)
    return ApprovalResult.APPROVED


def _side_effects_to_tool_kind(side_effects: list[SideEffect]) -> str:
    """Map Aar ``SideEffect`` list to an ACP ``ToolKind`` string.

    Returns the most "impactful" kind so Zed can pick the right icon.
    """
    if SideEffect.EXECUTE in side_effects:
        return "execute"
    if SideEffect.WRITE in side_effects:
        return "edit"
    if SideEffect.NETWORK in side_effects:
        return "fetch"
    if SideEffect.READ in side_effects:
        return "read"
    return "other"


def _extract_text(prompt_blocks: list) -> str:
    """Pull plain text out of ACP SDK prompt content blocks.

    Handles TextContentBlock, ResourceContentBlock (URI links), and
    EmbeddedResourceContentBlock (@ file context with embedded content).
    """
    parts: list[str] = []
    for block in prompt_blocks:
        if isinstance(block, dict):
            btype = block.get("type", "")
            text = block.get("text", "")
            if text:
                parts.append(text)
            elif btype == "resource":
                uri = block.get("uri", "")
                if uri:
                    parts.append(f"[resource: {uri}]")
        else:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
                continue
            uri = getattr(block, "uri", None)
            if uri:
                parts.append(f"[resource: {uri}]")
                continue
            resource = getattr(block, "resource", None)
            if resource is not None:
                res_text = getattr(resource, "text", None)
                res_uri = getattr(resource, "uri", None)
                if res_text:
                    header = f"[{res_uri}]" if res_uri else "[file]"
                    parts.append(f"{header}\n{res_text}")
                elif res_uri:
                    parts.append(f"[resource: {res_uri}]")
    return "\n".join(parts)


def _map_stop_reason(state: AgentState) -> str:
    """Map Aar ``AgentState`` to an ACP ``StopReason`` string.

    Valid ACP stop reasons: end_turn, max_tokens, max_turn_requests,
    refusal, cancelled.

    ``refusal`` triggers a prominent "refused to respond" banner in Zed,
    so we only use it for genuine content-policy refusals — NOT for
    operational failures like timeouts or budget limits.
    """
    if state == AgentState.MAX_STEPS:
        return "max_turn_requests"
    if state == AgentState.CANCELLED:
        return "cancelled"
    return "end_turn"


def _kv_list_to_dict(value: Any) -> dict[str, str]:
    """Coerce ACP name/value pair lists (``env``, ``headers``) to a ``dict``.

    ACP ships both fields as ``list[{name, value}]`` rather than ``dict``.
    Also accepts a dict for convenience (tests, dict-form server specs).
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    result: dict[str, str] = {}
    for item in value:
        if isinstance(item, dict):
            name = item.get("name")
            val = item.get("value")
        else:
            name = getattr(item, "name", None)
            val = getattr(item, "value", None)
        if name is not None and val is not None:
            result[str(name)] = str(val)
    return result


def _acp_server_to_mcp_config(srv: Any) -> Any:
    """Convert an ACP SDK MCP server object to an Aar MCPServerConfig, or None.

    Handles all three ACP server shapes:

    * ``McpServerStdio`` — required fields ``name, command, args, env``; ``env``
      is a ``list[EnvVariable]`` of ``{name, value}`` pairs.
    * ``HttpMcpServer`` — ``type == "http"``, ``url``, and
      ``headers: list[HttpHeader]``.
    * ``SseMcpServer`` — ``type == "sse"``. Aar's MCP bridge does not speak
      SSE framing; logs a warning and returns ``None`` so the server is
      skipped rather than silently mis-routed.

    Also accepts plain dicts for programmatic callers.
    """
    try:
        from agent.extensions.mcp import MCPServerConfig
    except ImportError:
        return None

    if isinstance(srv, dict):
        srv_type = srv.get("type")
        name = srv.get("name") or srv.get("command") or srv.get("url") or "mcp"
        if srv_type == "sse":
            logger.warning(
                "ACP: MCP server %r uses SSE transport which is not supported; skipping",
                name,
            )
            return None
        if srv_type == "http" or "url" in srv:
            return MCPServerConfig(
                name=str(name),
                transport="http",
                url=str(srv.get("url", "")),
                headers=_kv_list_to_dict(srv.get("headers")),
            )
        if "command" in srv:
            return MCPServerConfig(
                name=str(name),
                transport="stdio",
                command=str(srv.get("command", "")),
                args=list(srv.get("args", []) or []),
                env=_kv_list_to_dict(srv.get("env")),
            )
        return None

    srv_type = getattr(srv, "type", None)
    url = getattr(srv, "url", None)
    command = getattr(srv, "command", None)
    name = getattr(srv, "name", None) or (command or url or "mcp")

    if srv_type == "sse":
        logger.warning(
            "ACP: MCP server %r uses SSE transport which is not supported; skipping",
            name,
        )
        return None
    if srv_type == "http" or (url and not command):
        return MCPServerConfig(
            name=str(name),
            transport="http",
            url=str(url or ""),
            headers=_kv_list_to_dict(getattr(srv, "headers", None)),
        )
    if command:
        return MCPServerConfig(
            name=str(name),
            transport="stdio",
            command=str(command),
            args=list(getattr(srv, "args", []) or []),
            env=_kv_list_to_dict(getattr(srv, "env", None)),
        )
    return None


def _model_id_to_provider(model_id: str) -> tuple[str, str]:
    """Map a model ID string to (provider_name, model).

    Uses simple prefix matching so new model releases work automatically.
    Falls back to Ollama for unknown IDs (safe for local models).
    """
    mid = model_id.lower()
    if mid.startswith("claude"):
        return "anthropic", model_id
    if any(mid.startswith(p) for p in ("gpt-", "o1", "o3", "o4", "chatgpt")):
        return "openai", model_id
    return "ollama", model_id


def _available_commands() -> list[Any]:
    """Return the list of slash commands Aar exposes to ACP editors."""
    from acp.schema import AvailableCommand

    return [
        AvailableCommand(
            name="status",
            description="Show current session info: model, provider, step count, and session ID",
        ),
        AvailableCommand(
            name="tools",
            description="List all tools available in this session",
        ),
        AvailableCommand(
            name="policy",
            description="Show the active safety policy: approval mode and path restrictions",
        ),
    ]
