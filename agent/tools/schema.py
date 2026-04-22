"""Tool metadata and schema definitions."""

from __future__ import annotations

from enum import Enum
from typing import Any, Callable, Coroutine

from pydantic import BaseModel, Field


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
