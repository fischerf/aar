"""ACP transport — Agent Communication Protocol server for Aar.

Two transports, split into focused submodules:

- :mod:`.stdio` — ``AarAcpAgent`` + ``run_acp_stdio()`` (SDK-based stdio;
  for Zed and other ACP-compatible editors).
- :mod:`.http`  — ``AcpTransport`` + ``create_acp_asgi_app()`` (ACP v0.2
  over HTTP/SSE; for remote or programmatic clients).
- :mod:`.common` — helpers shared by both transports.

Spec: https://agentcommunicationprotocol.dev

The names re-exported here are the stable public surface plus the private
helpers that the test suite pokes at.
"""

from __future__ import annotations

from .common import (
    _acp_server_to_mcp_config,
    _auto_approve,
    _available_commands,
    _extract_text,
    _load_default_config,
    _map_stop_reason,
    _model_id_to_provider,
    _side_effects_to_tool_kind,
)
from .http import (
    AcpMessage,
    AcpRun,
    AcpSseEvent,
    AcpTransport,
    AgentManifest,
    MessageCreatedEvent,
    MessagePart,
    RunCancelledEvent,
    RunCompletedEvent,
    RunCreatedEvent,
    RunFailedEvent,
    RunInProgressEvent,
    RunMode,
    RunStatus,
    _collect_output,
    _sse_line,
    create_acp_asgi_app,
)
from .stdio import AarAcpAgent, run_acp_stdio

__all__ = [
    # Public — stdio
    "AarAcpAgent",
    "run_acp_stdio",
    # Public — HTTP
    "AcpTransport",
    "create_acp_asgi_app",
    # Public — HTTP data models
    "AcpMessage",
    "AcpRun",
    "AcpSseEvent",
    "AgentManifest",
    "MessagePart",
    "MessageCreatedEvent",
    "RunCancelledEvent",
    "RunCompletedEvent",
    "RunCreatedEvent",
    "RunFailedEvent",
    "RunInProgressEvent",
    "RunMode",
    "RunStatus",
    # Internal — re-exported for tests and advanced callers
    "_acp_server_to_mcp_config",
    "_auto_approve",
    "_available_commands",
    "_collect_output",
    "_extract_text",
    "_load_default_config",
    "_map_stop_reason",
    "_model_id_to_provider",
    "_side_effects_to_tool_kind",
    "_sse_line",
]
