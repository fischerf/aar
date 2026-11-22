"""Tool metadata and schema definitions."""

from __future__ import annotations

from enum import Enum
from typing import Any, Callable, Coroutine

from pydantic import BaseModel, Field

from agent.core.events import ImageURLBlock


class ToolOutput(BaseModel):
    """A tool result with images, for handlers that return more than text.

    ``text`` is what every model sees and should mention each image (the MCP
    bridge writes ``[image: image/png, 120 KB]``). ``images`` reach the model
    only when the active provider supports vision.
    """

    text: str = ""
    images: list[ImageURLBlock] = Field(default_factory=list)

    def __str__(self) -> str:
        return self.text


class SideEffect(str, Enum):
    NONE = "none"
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    NETWORK = "network"
    EXTERNAL = "external"  # tool executes via an external MCP server


class ToolSpec(BaseModel):
    """Metadata for a registered tool."""

    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    side_effects: list[SideEffect] = Field(default_factory=lambda: [SideEffect.NONE])
    requires_approval: bool = False

    # True for an EXECUTE tool that runs no shell command of its own and whose
    # work is checked call by call against the same SafetyPolicy (``spawn_agent``:
    # the child agent inherits the parent's safety config). Such a tool is exempt
    # from the forced ASK that guards unverifiable shell commands under
    # ``allowed_paths``; ``require_approval_for_execute`` still applies.
    policy_confined: bool = False

    # Per-tool override for the executor's outer timeout, in seconds. ``None``
    # falls back to ``ToolConfig.command_timeout``; a value <= 0 means "no outer
    # timeout". Set this on tools whose work legitimately outlives the shared
    # cap (a diffusion render, a sub-agent run) instead of raising the cap for
    # every tool in the process.
    timeout_s: int | None = None

    # One-line summary shown in the system prompt's "Available tools" section.
    prompt_snippet: str = ""
    # Conditional guidelines injected into the system prompt when this tool is active.
    prompt_guidelines: list[str] = Field(default_factory=list)

    # The actual callable (excluded from serialization)
    handler: Callable[..., Coroutine[Any, Any, str]] | Callable[..., str] | None = Field(
        default=None, exclude=True
    )

    def to_provider_schema(self) -> dict[str, Any]:
        """Convert to the Anthropic/OpenAI tool schema format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
