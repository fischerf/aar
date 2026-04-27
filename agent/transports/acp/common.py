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


def _side_effects_to_tool_kind(side_effects: list[SideEffect], tool_name: str = "") -> str:
    """Map Aar ``SideEffect`` list to an ACP ``ToolKind`` string.

    Returns the most "impactful" kind so Zed can pick the right icon.
    Name-based detection runs first to cover kinds (delete, move, search,
    think) that have no dedicated ``SideEffect`` value.
    """
    name = tool_name.lower()
    if any(kw in name for kw in ("delete", "remove", "unlink")):
        return "delete"
    if any(kw in name for kw in ("move", "rename")):
        return "move"
    if any(kw in name for kw in ("search", "grep", "find", "glob")):
        return "search"
    if any(kw in name for kw in ("think", "reason", "reflect")):
        return "think"
    if SideEffect.EXECUTE in side_effects:
        return "execute"
    if SideEffect.WRITE in side_effects:
        return "edit"
    if SideEffect.NETWORK in side_effects:
        return "fetch"
    if SideEffect.READ in side_effects:
        return "read"
    return "other"


_PATH_KEYS = ("path", "file_path", "filepath", "source", "destination", "target", "filename")


def _extract_locations(arguments: dict[str, Any]) -> list[str]:
    """Extract file-path strings from tool call arguments.

    Checks well-known parameter names and returns any non-empty string
    values found, preserving insertion order and skipping duplicates.
    """
    seen: set[str] = set()
    result: list[str] = []
    for key in _PATH_KEYS:
        val = arguments.get(key)
        if isinstance(val, str) and val and val not in seen:
            seen.add(val)
            result.append(val)
    return result


def _extract_text(prompt_blocks: list) -> str:
    """Pull plain text out of ACP SDK prompt content blocks.

    Handles TextContentBlock, ResourceContentBlock (URI links),
    EmbeddedResourceContentBlock (@ file context with embedded content),
    and image/audio blocks (replaced with a short placeholder).
    """
    parts: list[str] = []
    for block in prompt_blocks:
        if isinstance(block, dict):
            btype = block.get("type", "")
            text = block.get("text", "")
            if text:
                parts.append(text)
            elif btype == "image":
                parts.append("[image]")
            elif btype == "audio":
                parts.append("[audio]")
            elif btype == "resource":
                uri = block.get("uri", "")
                if uri:
                    parts.append(f"[resource: {uri}]")
        else:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
                continue
            btype = getattr(block, "type", "") or ""
            if btype == "image" or (
                not btype and hasattr(block, "data") and hasattr(block, "mime_type")
            ):
                parts.append("[image]")
                continue
            if btype == "audio":
                parts.append("[audio]")
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
    if state == AgentState.BUDGET_EXCEEDED:
        return "max_tokens"
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

    Accepts explicit ``provider/model`` format (e.g. ``openai/gpt-4o``) or
    uses prefix matching for bare model names.  Falls back to Ollama for
    unknown IDs (safe for local models).
    """
    if "/" in model_id:
        provider_name, model = model_id.split("/", 1)
        provider_name = provider_name.lower()
        if provider_name in {"anthropic", "openai", "ollama", "gemini", "generic"}:
            return provider_name, model
    mid = model_id.lower()
    if mid.startswith("claude"):
        return "anthropic", model_id
    if any(mid.startswith(p) for p in ("gpt-", "o1", "o3", "o4", "chatgpt")):
        return "openai", model_id
    if mid.startswith("gemini"):
        return "gemini", model_id
    return "ollama", model_id


def _available_commands(extra: dict[str, str] | None = None) -> list[Any]:
    """Return the list of slash commands Aar exposes to ACP editors.

    *extra* maps command name → description for extension-registered commands.
    """
    from acp.schema import AvailableCommand

    cmds = [
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
    if extra:
        for name, desc in extra.items():
            cmds.append(AvailableCommand(name=name, description=desc or ""))
    return cmds


def _derive_mode_id(safety_cfg: Any, current_mode_id: str | None = None) -> str:
    """Derive the active mode ID from the safety config.

    Returns *current_mode_id* unchanged when provided (explicit override).
    Falls back to inferring the mode from the safety flags:

    * ``read-only`` — when ``safety.read_only`` is set
    * ``auto``      — when both approval flags are off
    * ``review``    — otherwise (safe default)
    """
    if current_mode_id is not None:
        return current_mode_id
    if getattr(safety_cfg, "read_only", False):
        return "read-only"
    if not (
        getattr(safety_cfg, "require_approval_for_writes", True)
        or getattr(safety_cfg, "require_approval_for_execute", True)
    ):
        return "auto"
    return "review"


def _build_mode_state(safety_cfg: Any, current_mode_id: str | None = None) -> Any:
    """Return a ``SessionModeState`` advertising the three Aar modes.

    Kept for backwards compatibility — newer clients use ``configOptions``
    (``category="mode"``).  The current mode is derived via
    :func:`_derive_mode_id`.
    """
    from acp.schema import SessionMode, SessionModeState

    modes = [
        SessionMode(
            id="auto",
            name="Auto",
            description="Run writes and shell commands without asking for approval.",
        ),
        SessionMode(
            id="review",
            name="Review",
            description="Ask for approval before writes and shell commands.",
        ),
        SessionMode(
            id="read-only",
            name="Read-only",
            description="Only read-only operations; writes and shell commands are denied.",
        ),
    ]

    return SessionModeState(
        available_modes=modes,
        current_mode_id=_derive_mode_id(safety_cfg, current_mode_id),
    )


def _build_config_options(
    safety_cfg: Any,
    provider_cfg: Any = None,
    current_mode_id: str | None = None,
) -> list[Any]:
    """Build the ``configOptions`` list for session setup and ``set_config_option`` responses.

    Returns options in priority order (per ACP spec, order drives prominent placement):

    1. **Model** select  (``category="model"``) — only the active model from config;
       no built-in catalogue so the picker shows exactly what ``config.json`` has wired up.
    2. **Mode** select   (``category="mode"``)  — ``auto`` / ``review`` / ``read-only``.
    3. ``auto_approve_writes`` boolean toggle.
    4. ``auto_approve_execute`` boolean toggle.
    5. ``read_only`` boolean toggle.

    Clients that support ``configOptions`` SHOULD use these and ignore the
    separate ``modes`` field (which is kept only for older client compatibility).
    """
    from acp.schema import (
        SessionConfigOptionSelect,
        SessionConfigSelectOption,
    )

    opts: list[Any] = []

    # 1. Model picker (category="model", type="select") — active model only.
    if provider_cfg is not None:
        model_id = str(getattr(provider_cfg, "model", "") or "aar")
        provider_name = str(getattr(provider_cfg, "name", "") or "")
        opts.append(
            SessionConfigOptionSelect(
                id="model",
                name="Model",
                type="select",
                category="model",
                description="The AI model to use for this session.",
                current_value=model_id,
                options=[
                    SessionConfigSelectOption(
                        value=model_id,
                        name=model_id,
                        description=f"Active model ({provider_name})"
                        if provider_name
                        else "Active model",
                    )
                ],
            )
        )

    # 2. Mode picker (category="mode", type="select").
    current_mode = _derive_mode_id(safety_cfg, current_mode_id)
    opts.append(
        SessionConfigOptionSelect(
            id="mode",
            name="Mode",
            type="select",
            category="mode",
            description="Controls approval behavior for write and execute tool calls.",
            current_value=current_mode,
            options=[
                SessionConfigSelectOption(
                    value="auto",
                    name="Auto",
                    description="Run writes and shell commands without asking for approval.",
                ),
                SessionConfigSelectOption(
                    value="review",
                    name="Review",
                    description="Ask for approval before writes and shell commands.",
                ),
                SessionConfigSelectOption(
                    value="read-only",
                    name="Read-only",
                    description="Only read-only operations; writes and shell commands are denied.",
                ),
            ],
        )
    )

    logger.debug(
        "ACP: config_options built: %s",
        [getattr(o, "id", "?") for o in opts],
    )
    return opts


def _strip_line_numbers(text: str) -> str:
    """Remove the ``read_file`` line-number prefix from numbered output.

    ``read_file`` returns lines formatted as ``{n:>6}\\t{line}`` so that
    the model can reference exact line numbers.  That prefix is useful for
    the LLM but is visual noise in Zed's tool-call panel, so we strip it
    before building the display content block.

    Lines that don't match the pattern (e.g. shell output) are left as-is.
    """
    import re

    return re.sub(r"^ *\d+\t", "", text, flags=re.MULTILINE)


def _path_to_file_uri(path: str) -> str:
    """Convert an absolute filesystem path to a ``file://`` URI.

    Handles both Windows paths (``C:\\foo\\bar`` → ``file:///C:/foo/bar``)
    and POSIX paths (``/foo/bar`` → ``file:///foo/bar``).
    """
    from pathlib import Path
    from urllib.parse import quote

    abs_path = str(Path(path).resolve())
    unix = abs_path.replace("\\", "/")
    if not unix.startswith("/"):
        unix = "/" + unix
    # RFC 3986 unreserved + sub-delims + "@" are all safe in path segments;
    # include them so common characters like "!" are not percent-encoded.
    return "file://" + quote(unix, safe="/:!$&'()*+,;=@~.-_")


def _guess_mime_type(path: str) -> str:
    """Return a MIME type for *path* based on its file extension.

    Falls back to ``"text/plain"`` when the type cannot be determined.
    """
    import mimetypes

    mime, _ = mimetypes.guess_type(path)
    return mime or "text/plain"


def _build_tool_result_content(
    tool_name: str,
    arguments: dict[str, Any],
    output: str,
    is_error: bool,
) -> tuple[list[Any], dict[str, Any]]:
    """Build ACP ToolCallContent blocks and a matching raw_output dict.

    Returns ``(content_blocks, raw_output)`` so callers can set both fields
    on ``ToolCallProgress`` consistently.

    * read_file  → EmbeddedResourceContentBlock (file URI + MIME type)
    * edit_file  → FileEditToolCallContent       (diff: old_string vs new_string)
    * write_file → FileEditToolCallContent       (diff: None vs full content)
    * everything else / errors
                 → ContentToolCallContent        (plain text block)

    ``raw_output`` mirrors the display text so both fields stay in sync:
    - read_file:  stripped clean text (no line-number prefix)
    - edit_file / write_file: the short confirmation message from the tool
    - everything else: same plain text (capped at 4 000 chars)
    """
    from pathlib import Path

    from acp.schema import (
        ContentToolCallContent,
        EmbeddedResourceContentBlock,
        FileEditToolCallContent,
        TextContentBlock,
        TextResourceContents,
    )

    _MAX = 4000

    def _text_block(text: str) -> ContentToolCallContent:
        return ContentToolCallContent(
            type="content",
            content=TextContentBlock(type="text", text=text[:_MAX]),
        )

    if is_error:
        capped = output[:_MAX]
        return [_text_block(capped)], {"text": capped}

    name = tool_name.lower()

    if name == "read_file":
        path = arguments.get("path", "")
        clean = _strip_line_numbers(output)
        uri = _path_to_file_uri(path) if path else "file:///unknown"
        mime = _guess_mime_type(path) if path else "text/plain"
        block = ContentToolCallContent(
            type="content",
            content=EmbeddedResourceContentBlock(
                type="resource",
                resource=TextResourceContents(
                    uri=uri,
                    mime_type=mime,
                    text=clean[:_MAX],
                ),
            ),
        )
        return [block], {"text": clean[:_MAX]}

    if name == "edit_file":
        path = arguments.get("path", "")
        old_string = arguments.get("old_string", "")
        new_string = arguments.get("new_string", "")
        if path:
            capped_out = output[:_MAX]
            return [
                FileEditToolCallContent(
                    type="diff",
                    path=str(Path(path).resolve()),
                    old_text=old_string or None,
                    new_text=new_string,
                )
            ], {"text": capped_out}

    if name == "write_file":
        path = arguments.get("path", "")
        content = arguments.get("content", "")
        if path:
            capped_out = output[:_MAX]
            return [
                FileEditToolCallContent(
                    type="diff",
                    path=str(Path(path).resolve()),
                    old_text=None,
                    new_text=content[:_MAX],
                )
            ], {"text": capped_out}

    # Default: plain text for list_directory, shell, etc.
    capped = output[:_MAX]
    return [_text_block(capped)], {"text": capped}
